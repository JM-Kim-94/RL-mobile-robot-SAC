#!/usr/bin/env python3


import rospy
import numpy as np
# np.random.bit_generator = np.random._bit_generator
import matplotlib.pyplot as plt
import random
import time
import sys
import os
import shutil
sys.path.append(os.path.dirname(os.path.abspath(os.path.dirname(__file__))))
from collections import deque
from collections import namedtuple
from std_msgs.msg import Float32MultiArray
import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torch.autograd import Variable
from torch.distributions import Categorical
from torch.distributions import Normal


import GPUtil
import psutil
from threading import Thread
import time

from env_mobile_robot_SAC_new_test import Env

# from torch.utils.tensorboard import SummaryWriter
from tensorboardX import SummaryWriter

from std_srvs.srv import Empty

from collections import namedtuple
import collections, random


class ReplayBuffer():
    def __init__(self, buffer_limit, DEVICE):
        self.buffer = deque(maxlen=buffer_limit)
        self.dev = DEVICE

    def put(self, transition):
        self.buffer.append(transition)

    def sample(self, n):
        mini_batch = random.sample(self.buffer, n)
        s_lst, a_lst, r_lst, s_prime_lst, done_mask_lst = [], [], [], [], []

        for transition in mini_batch:
            s, a, r, s_prime, done = transition
            s_lst.append(s)
            a_lst.append(a)
            r_lst.append([r])
            s_prime_lst.append(s_prime)
            done_mask = 0.0 if done else 1.0
            done_mask_lst.append([done_mask])

        s_batch = torch.tensor(s_lst, dtype=torch.float).to(self.dev)
        a_batch = torch.tensor(a_lst, dtype=torch.float).to(self.dev)
        r_batch = torch.tensor(r_lst, dtype=torch.float).to(self.dev)
        s_prime_batch = torch.tensor(s_prime_lst, dtype=torch.float).to(self.dev)
        done_batch = torch.tensor(done_mask_lst, dtype=torch.float).to(self.dev)

        # r_batch = (r_batch - r_batch.mean()) / (r_batch.std() + 1e-7)

        return s_batch, a_batch, r_batch, s_prime_batch, done_batch

    def size(self):
        return len(self.buffer)
        
        
class PolicyNetwork(nn.Module):
    def __init__(self, state_dim, action_dim, actor_lr, DEVICE):
        super(PolicyNetwork, self).__init__()

        self.fc_1 = nn.Linear(state_dim, 128)
        self.fc_2 = nn.Linear(128, 128)
        self.fc_3 = nn.Linear(128, 64)
        self.fc_mu = nn.Linear(64, action_dim)
        self.fc_std = nn.Linear(64, action_dim)

        self.lr = actor_lr
        self.dev = DEVICE

        self.LOG_STD_MIN = -20
        self.LOG_STD_MAX = 2
        self.max_action = torch.FloatTensor([1.5, 0.5]).to(self.dev)
        self.min_action = torch.FloatTensor([-1.5, 0]).to(self.dev)
        self.action_scale = (self.max_action - self.min_action) / 2.0
        self.action_bias = (self.max_action + self.min_action) / 2.0
        
        self.to(self.dev)

        self.optimizer = optim.Adam(self.parameters(), lr=self.lr)

    def forward(self, x):
        x = F.leaky_relu(self.fc_1(x))
        x = F.leaky_relu(self.fc_2(x))
        x = F.leaky_relu(self.fc_3(x))
        mu = self.fc_mu(x)
        log_std = self.fc_std(x)
        log_std = torch.clamp(log_std, self.LOG_STD_MIN, self.LOG_STD_MAX)
        return mu, log_std

    def sample(self, state):
        mean, log_std = self.forward(state)
        std = torch.exp(log_std)
        reparameter = Normal(mean, std)
        x_t = reparameter.rsample()
        y_t = torch.tanh(x_t)
        action = self.action_scale * y_t + self.action_bias

        # # Enforcing Action Bound
        log_prob = reparameter.log_prob(x_t)
        log_prob = log_prob - torch.sum(torch.log(self.action_scale * (1 - y_t.pow(2)) + 1e-6), dim=-1, keepdim=True)

        return action, log_prob
        
        
        

class QNetwork(nn.Module):
    def __init__(self, state_dim, action_dim, critic_lr, DEVICE):
        super(QNetwork, self).__init__()

        self.fc_s = nn.Linear(state_dim, 64)
        self.fc_a = nn.Linear(action_dim, 64)
        self.fc_1 = nn.Linear(128, 128)
        self.fc_2 = nn.Linear(128, 64)
        self.fc_out = nn.Linear(64, action_dim)

        self.lr = critic_lr
        self.dev = DEVICE
        
        self.to(self.dev)

        self.optimizer = optim.Adam(self.parameters(), lr=self.lr)

    def forward(self, x, a):
        h1 = F.leaky_relu(self.fc_s(x))
        h2 = F.leaky_relu(self.fc_a(a))
        cat = torch.cat([h1, h2], dim=-1)
        q = F.leaky_relu(self.fc_1(cat))
        q = F.leaky_relu(self.fc_2(q))
        q = self.fc_out(q)
        return q



class SAC_Agent:
    def __init__(self):
        self.state_dim      = 4  
        self.action_dim     = 2  
        self.lr_pi          = 0.0001
        self.lr_q           = 0.0005
        self.gamma          = 0.98
        self.batch_size     = 200
        self.buffer_limit   = 500000
        self.tau            = 0.0007   # for soft-update of Q using Q-target
        self.init_alpha     = 8
        self.target_entropy = -2 #-self.action_dim  # == -2
        self.lr_alpha       = 0.001
        self.DEVICE         = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.memory         = ReplayBuffer(self.buffer_limit, self.DEVICE)
        print("사용 장치 : ", self.DEVICE)

        self.log_alpha = torch.tensor(np.log(self.init_alpha)).to(self.DEVICE)
        self.log_alpha.requires_grad = True
        self.log_alpha_optimizer = optim.Adam([self.log_alpha], lr=self.lr_alpha)

        self.PI  = PolicyNetwork(self.state_dim, self.action_dim, self.lr_pi, self.DEVICE)
        self.Q1        = QNetwork(self.state_dim, self.action_dim, self.lr_q, self.DEVICE)
        self.Q1_target = QNetwork(self.state_dim, self.action_dim, self.lr_q, self.DEVICE)
        self.Q2        = QNetwork(self.state_dim, self.action_dim, self.lr_q, self.DEVICE)
        self.Q2_target = QNetwork(self.state_dim, self.action_dim, self.lr_q, self.DEVICE)

        self.Q1_target.load_state_dict(self.Q1.state_dict())
        self.Q2_target.load_state_dict(self.Q2.state_dict())

    def choose_action(self, s):
        with torch.no_grad():
            action, log_prob = self.PI.sample(s.to(self.DEVICE))
        return action, log_prob

    def calc_target(self, mini_batch):
        s, a, r, s_prime, done = mini_batch
        with torch.no_grad():
            a_prime, log_prob_prime = self.PI.sample(s_prime)
            entropy = - self.log_alpha.exp() * log_prob_prime
            q1_target, q2_target = self.Q1_target(s_prime, a_prime), self.Q2_target(s_prime, a_prime)
            q_target = torch.min(q1_target, q2_target)
            target = r + self.gamma * done * (q_target + entropy)
        return target

    def train_agent(self):
        mini_batch = self.memory.sample(self.batch_size)
        s_batch, a_batch, r_batch, s_prime_batch, done_batch = mini_batch

        td_target = self.calc_target(mini_batch)

        #### Q1 train ####
        q1_loss = F.smooth_l1_loss(self.Q1(s_batch, a_batch), td_target)
        self.Q1.optimizer.zero_grad()
        q1_loss.mean().backward()
        nn.utils.clip_grad_norm_(self.Q1.parameters(), 1.0)
        self.Q1.optimizer.step()
        #### Q1 train ####

        #### Q2 train ####
        q2_loss = F.smooth_l1_loss(self.Q2(s_batch, a_batch), td_target)
        self.Q2.optimizer.zero_grad()
        q2_loss.mean().backward()
        nn.utils.clip_grad_norm_(self.Q2.parameters(), 1.0)
        self.Q2.optimizer.step()
        #### Q2 train ####

        #### pi train ####
        a, log_prob = self.PI.sample(s_batch)
        entropy = -self.log_alpha.exp() * log_prob

        q1, q2 = self.Q1(s_batch, a), self.Q2(s_batch, a)
        q = torch.min(q1, q2)

        pi_loss = -(q + entropy)  # for gradient ascent
        self.PI.optimizer.zero_grad()
        pi_loss.mean().backward()
        nn.utils.clip_grad_norm_(self.PI.parameters(), 1.0)
        self.PI.optimizer.step()
        #### pi train ####

        #### alpha train ####
        self.log_alpha_optimizer.zero_grad()
        alpha_loss = -(self.log_alpha.exp() * (log_prob + self.target_entropy).detach()).mean()
        alpha_loss.backward()
        self.log_alpha_optimizer.step()
        #### alpha train ####

        #### Q1, Q2 soft-update ####
        for param_target, param in zip(self.Q1_target.parameters(), self.Q1.parameters()):
            param_target.data.copy_(param_target.data * (1.0 - self.tau) + param.data * self.tau)
        for param_target, param in zip(self.Q2_target.parameters(), self.Q2.parameters()):
            param_target.data.copy_(param_target.data * (1.0 - self.tau) + param.data * self.tau)
        #### Q1, Q2 soft-update ####
        



class Monitor(Thread):
    def __init__(self, delay):
        super(Monitor, self).__init__()
        self.stopped = False
        self.delay = delay # Time between calls to GPUtil
        self.start()

    def run(self):
        while not self.stopped:
            GPUtil.showUtilization()
            print("CPU percentage: ", psutil.cpu_percent())
            print('CPU virtual_memory used:', psutil.virtual_memory()[2], "\n")
            time.sleep(self.delay)

    def stop(self):
        self.stopped = True
        



if __name__ == '__main__':
    rospy.init_node('mobile_robot_sac')
    
    GPU_CPU_monitor = Monitor(60)
    
    date = '0213'
    save_dir = "/home/jm-kim/catkin_ws/src/myrobot/src/SAC/saved_model/" + date 
    if not os.path.isdir(save_dir):
        os.mkdir(save_dir)
    save_dir += "/"
        
    shutil.copyfile('/home/jm-kim/catkin_ws/src/myrobot/src/SAC/env_mobile_robot_SAC_new.py', save_dir+'env.py')
    shutil.copyfile('/home/jm-kim/catkin_ws/src/myrobot/src/SAC/mobile_robot_SAC_2act_new.py', save_dir+'agt.py')   
    
    writer = SummaryWriter('SAC_log/'+date)
    
    env = Env()
    agent = SAC_Agent()
    
    EPISODE = 2000
    MAX_STEP_SIZE = 3000
    
    sim_rate = rospy.Rate(100)   

    score = 0.0
    print_once = True

    for EP in range(EPISODE):
        state = env.reset(EP)
        score, done = 0.0 , False        
        
        for step in range(MAX_STEP_SIZE): #while not done:
            action, log_prob = agent.choose_action(torch.FloatTensor(state))
            action = action.detach().cpu().numpy()
            
            #print(a)
            #action = [a[0]*1.5 , (a[1]+1)/4]
            
            state_prime, reward, done, arrival = env.step(action, EP)
            
            agent.memory.put((state, action, reward, state_prime, done))
            
            score += reward
            
            state = state_prime
            
            if done:
                state = env.reset(EP)
             
        
            if agent.memory.size()>10000:
                if print_once: 
                    print("학습시작!")
                    print_once = False
                agent.train_agent()
                
            if agent.memory.size()<=10000:
                # sim_rate.sleep()   
                time.sleep(0.002)      
        
          
        writer.add_scalar("Score", score, EP)    
        print("EP:{}, Avg_Score:{:.1f}".format(EP, score), "\n")  
        #print("EP:{}, Avg_Score:{:.1f}, Q:{:.1f}, Entr:{:.1f}, Act_los:{:.1f}, Alpha:{:.3f}".format(n_epi, score, q/3000, ent/3000, actor_loss/3000, pi.log_alpha.exp()))
        #writer.add_scalar("Q_Value", q/3000, n_epi)
        #writer.add_scalar("Entropy", ent/3000 ,n_epi)
        #writer.add_scalar("Actor_Loss", actor_loss/3000 ,n_epi)
        #writer.add_scalar("alpha", alpha/3000 ,n_epi)
        
        #if pi.log_alpha.exp() > 1.0:
        #    pi.log_alpha += np.log(0.99)
        
        #if n_epi%print_interval==0 and n_epi!=0:
        #    print("# of episode :{}, avg score : {:.1f} alpha:{:.4f}".format(n_epi, score/print_interval, pi.log_alpha.exp()))
        #    writer.add_scalar("Score", score / print_interval, n_epi)
        #    score = 0.0
            
        if EP % 2 == 0: 
            torch.save(agent.PI.state_dict(), save_dir + "sac_actor_"+date+"_EP"+str(EP)+".pt")










    
    
    
    
    
    
    
    
    
    
    
    
    

