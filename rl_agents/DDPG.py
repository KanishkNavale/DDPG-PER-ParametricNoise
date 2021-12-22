# Library Imports
import numpy as np
import torch as T
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from replay_buffers.PER import PrioritizedReplayBuffer
from replay_buffers.utils import LinearSchedule
T.manual_seed(0)
np.random.seed(0)


device = T.device("cuda:0" if T.cuda.is_available() else "cpu:0")


class OUNoise:
    def __init__(self, action_dimension, scale=0.1, mu=0, theta=0.15, sigma=0.2):
        self.action_dimension = action_dimension
        self.scale = scale
        self.mu = mu
        self.theta = theta
        self.sigma = sigma
        self.state = np.ones(self.action_dimension) * self.mu
        self.reset()

    def reset(self):
        self.state = np.ones(self.action_dimension) * self.mu

    def __call__(self):
        x = self.state
        dx = self.theta * (self.mu - x) + self.sigma * np.random.randn(len(x))
        self.state = x + dx
        return self.state * self.scale


class Critic(nn.Module):
    """Defines a Critic Deep Learning Network"""

    def __init__(self, input_dim, beta, density=32, name='critic'):
        super(Critic, self).__init__()

        self.model_name = name
        self.checkpoint = self.model_name

        self.H1 = nn.Linear(input_dim, density)
        self.H2 = nn.Linear(density, density)
        self.H3 = nn.Linear(density, density)
        self.H4 = nn.Linear(density, density)
        self.Q = nn.Linear(density, 1)

        self.optimizer = optim.Adam(self.parameters(), lr=beta)
        self.to(device)

    def forward(self, state, action):
        value = T.hstack((state, action))
        value = F.relu(self.H1(value))
        value = F.relu(self.H2(value))
        value = F.relu(self.H3(value))
        value = F.relu(self.H4(value))
        value = self.Q(value)
        return value

    def save_model(self, path):
        T.save(self.state_dict(), path + self.checkpoint)

    def load_model(self, path):
        self.load_state_dict(T.load(path + self.checkpoint))


class Actor(nn.Module):
    """Defines a Actor Deep Learning Network"""

    def __init__(self, input_dim, n_actions, alpha, density=32, name='actor'):
        super(Actor, self).__init__()

        self.model_name = name
        self.checkpoint = self.model_name

        self.H1 = nn.Linear(input_dim, density)
        self.H2 = nn.Linear(density, density)
        self.H3 = nn.Linear(density, density)
        self.H4 = nn.Linear(density, density)
        self.mu = nn.Linear(density, n_actions)

        self.optimizer = optim.Adam(self.parameters(), lr=alpha)
        self.to(device)

    def forward(self, state):
        action = F.relu(self.H1(state))
        action = F.relu(self.H2(action))
        action = F.relu(self.H3(action))
        action = F.relu(self.H4(action))
        action = T.tanh(self.mu(action))
        return action

    def save_model(self, path):
        T.save(self.state_dict(), path + self.checkpoint)

    def load_model(self, path):
        self.load_state_dict(T.load(path + self.checkpoint))


class Agent:
    def __init__(self, env, datapath, n_games, alpha=0.001,
                 beta=0.002, gamma=0.99, tau=0.01, batch_size=64,
                 noise='param', per_alpha=0.6, per_beta=0.4):

        self.env = env
        self.gamma = T.tensor(gamma, dtype=T.float32).to(device)
        self.tau = T.tensor(tau, dtype=T.float32).to(device)
        self.n_actions = env.action_space.shape[0]
        self.obs_shape = env.observation_space.shape[0]
        self.datapath = datapath
        self.n_games = n_games
        self.optim_steps = 0
        self.max_size = 25000
        self.memory = PrioritizedReplayBuffer(self.max_size, per_alpha)
        self.beta_scheduler = LinearSchedule(n_games, per_beta, 0.7)

        self.batch_size = batch_size
        self.noise = noise
        self.max_action = env.action_space.high[0]
        self.min_action = env.action_space.low[0]

        self.actor = Actor(self.obs_shape, self.n_actions, alpha, name='actor')
        self.critic = Critic(self.obs_shape + self.n_actions, beta, name='critic')
        self.target_actor = Actor(self.obs_shape, self.n_actions, alpha, name='target_actor')
        self.target_critic = Critic(self.obs_shape + self.n_actions, beta, name='target_critic')

        if self.noise == 'normal':
            self.noise_param = 0.1

        elif self.noise == 'ou':
            self.noise = OUNoise(self.n_actions)

        elif self.noise == 'param':
            self.distances = []
            self.scalar = 0.01
            self.scalar_decay = 0.1
            self.desired_distance = 0.1
            self.noisy_actor = Actor(self.obs_shape, self.n_actions, alpha, name='noisy_actor')

        self.update_networks()

    def update_networks(self):
        tau = self.tau

        for critic_weights, target_critic_weights in zip(self.critic.parameters(), self.target_critic.parameters()):
            target_critic_weights.data.copy_(tau * critic_weights.data + (1 - tau) * target_critic_weights.data)

        for actor_weights, target_actor_weights in zip(self.actor.parameters(), self.target_actor.parameters()):
            target_actor_weights.data.copy_(tau * actor_weights.data + (1 - tau) * target_actor_weights.data)

    def update_noisy_actor(self):
        with T.no_grad():
            for actor_weights, noisy_actor_weights in zip(self.actor.parameters(), self.noisy_actor.parameters()):
                shape = actor_weights
                noise = T.normal(mean=T.zeros_like(shape), std=self.scalar * T.ones_like(shape)).to(device)
                noisy_actor_weights.data.copy_(actor_weights.data + noise)

    def store(self, state, action, reward, new_state, done):
        self.memory.add(state, action, reward, new_state, done)

    def choose_action(self, observation):
        self.actor.eval()
        state = T.tensor(observation, dtype=T.float32).to(device)
        action = self.actor.forward(state).to(device)

        if self.noise == 'normal':
            action += T.normal(mean=T.zeros_like(action, dtype=T.float32), std=self.noise * T.ones_like(action, dtype=T.float32)).to(device)

        elif self.noise == 'ou':
            action += T.tensor(self.noise(), dtype=T.float32).to(device)

        elif self.noise == 'param':
            self.update_noisy_actor()
            self.noisy_actor.eval()
            action_noised = self.noisy_actor(state)
            distance = T.linalg.norm(action - action_noised)
            self.distances.append(distance)
            if distance > self.desired_distance:
                self.scalar *= self.scalar_decay
            if distance < self.desired_distance:
                self.scalar /= self.scalar_decay
            action += action_noised

        action = T.clamp(action, self.min_action, self.max_action)
        self.actor.train()
        return action.detach().cpu().numpy()

    def save_models(self):
        self.actor.save_model(self.datapath)
        self.actor.save_model(self.datapath)
        self.target_actor.save_model(self.datapath)
        self.critic.save_model(self.datapath)
        self.target_critic.save_model(self.datapath)

    def load_models(self):
        self.actor.load_model(self.datapath)
        self.actor.load_model(self.datapath)
        self.target_actor.load_model(self.datapath)
        self.critic.load_model(self.datapath)
        self.target_critic.load_model(self.datapath)

    def optimize(self):
        if len(self.memory._storage) < self.batch_size:
            return

        beta = self.beta_scheduler.value(self.optim_steps)
        state, action, reward, new_state, done, weights, indices = self.memory.sample(self.batch_size, beta)

        state = T.tensor(np.vstack(state), dtype=T.float32).to(device)
        action = T.tensor(np.vstack(action), dtype=T.float32).to(device)
        done = T.tensor(np.vstack(1 - done), dtype=T.float32).to(device)
        reward = T.tensor(np.vstack(reward), dtype=T.float32).to(device)
        new_state = T.tensor(np.vstack(new_state), dtype=T.float32).to(device)
        weights = T.tensor(np.vstack(weights), dtype=T.float32).to(device)

        self.target_actor.eval()
        self.target_critic.eval()
        self.critic.eval()

        Q_target = self.target_critic(new_state, self.target_actor(new_state))
        Y = reward + (done * self.gamma * Q_target)
        Q = self.critic(state, action)
        TD_errors = T.sub(Y, Q)

        # Weight TD errors
        weighted_TD_errors = T.mul(TD_errors, weights)
        zero_tensor = T.zeros_like(weighted_TD_errors, dtype=T.float32).to(device)

        # Compute & Update Critic losses
        critic_loss = F.mse_loss(weighted_TD_errors, zero_tensor).to(device)
        self.critic.optimizer.zero_grad()
        critic_loss.backward()
        self.critic.optimizer.step()

        # Compute & Update Actor losses
        actor_loss = -self.critic(state, self.actor(state)).mean()
        self.actor.optimizer.zero_grad()
        actor_loss.backward()
        self.actor.optimizer.step()

        td_errors = TD_errors.detach().cpu().numpy()
        new_priorities = np.abs(td_errors) + 0.001
        self.memory.update_priorities(indices, new_priorities)

        self.update_networks()

        self.optim_steps += 1.0