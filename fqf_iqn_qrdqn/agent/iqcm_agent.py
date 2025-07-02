import torch
from torch.optim import Adam
import torch.nn.functional as F

from fqf_iqn_qrdqn.model.alpha_iqn import IQN
from fqf_iqn_qrdqn.model.mean import MeanNetwork
from fqf_iqn_qrdqn.utils import calculate_quantile_huber_loss, disable_gradients, evaluate_quantile_at_action, update_params

from .base_agent import BaseAgent


class IQCMAgent(BaseAgent):

    def __init__(self, env, valid_calculator, test_calculator, log_dir, num_steps=5*(10**7),
                 std_lam = 1, skew_lam = 0, kurt_lam = 0, 
                 batch_size=32, N=64, N_dash=64, K=32, num_cosines=64,
                 kappa=1.0, lr=5e-5, memory_size=10**6, gamma=0.99,
                 multi_step=1, update_interval=4, target_update_interval=10000,
                 start_steps=50000, epsilon_train=0.01, epsilon_eval=0.001,
                 epsilon_decay_steps=250000, double_q_learning=False,
                 dueling_net=False, noisy_net=False, use_per=False,
                 log_interval=100, eval_interval=250000, num_eval_steps=125000,
                 max_episode_steps=27000, grad_cliping=None, cuda=True,
                 seed=0):
        super(IQCMAgent, self).__init__(
            env, valid_calculator, test_calculator, log_dir, num_steps, batch_size, memory_size,
            gamma, multi_step, update_interval, target_update_interval,
            start_steps, epsilon_train, epsilon_eval, epsilon_decay_steps,
            double_q_learning, dueling_net, noisy_net, use_per, log_interval,
            eval_interval, num_eval_steps, max_episode_steps, grad_cliping,
            cuda, seed)

        # Online network.
        self.online_net = IQN(num_actions=self.num_actions,
                              K=K, 
                              num_cosines=num_cosines,
                              dueling_net=dueling_net, 
                              noisy_net=noisy_net,
                              require_QCM=True).to(self.device)
        # Target network.
        self.target_net = IQN(num_actions=self.num_actions,
                              K=K, 
                              num_cosines=num_cosines, 
                              dueling_net=dueling_net,
                              noisy_net=noisy_net,
                              require_QCM=True).to(self.device)
        
        self.online_mean_net = MeanNetwork(num_actions=self.num_actions,
                                           dueling_net=dueling_net,
                                           noisy_net=noisy_net).to(self.device)
        
        self.target_mean_net = MeanNetwork(num_actions=self.num_actions,
                                           dueling_net=dueling_net,
                                           noisy_net=noisy_net).to(self.device)

        # Copy parameters of the learning network to the target network.
        self.update_target()
        self.update_mean_target()
        # Disable calculations of gradients of the target network.
        disable_gradients(self.target_net)
        disable_gradients(self.target_mean_net)

        self.optim = Adam(self.online_net.parameters(), lr=lr, eps=1e-2/batch_size)
        self.mean_optim = Adam(self.online_mean_net.parameters(), lr=lr, eps=1e-2/batch_size)

        self.N = N
        self.N_dash = N_dash
        self.K = K
        self.num_cosines = num_cosines
        self.kappa = kappa
        self.std_lam = std_lam
        self.skew_lam = skew_lam
        self.kurt_lam = kurt_lam
        
    def update_mean_target(self):
        self.target_mean_net.load_state_dict(self.online_mean_net.state_dict())

    def exploit(self, state):
        # Act with bonus.
        state = torch.ByteTensor(state).unsqueeze(0).to(self.device).float()
        with torch.no_grad():
            q_values = self.online_mean_net.calculate_q(states=state)
            std, skewness, kurtosis = self.online_net.calculate_higher_moments(states = state)
            action_values = q_values + self.std_lam * std + self.skew_lam * skewness + self.kurt_lam * kurtosis
            
            forbid_action = torch.BoolTensor(~self.env.action_masks()).to(self.device)
            action_values[:, forbid_action] = -1e6
            action = action_values.argmax().item()
        return action
    
    def learn(self):
        self.learning_steps += 1
        self.online_net.sample_noise()
        self.target_net.sample_noise()
        self.online_mean_net.sample_noise()
        self.target_mean_net.sample_noise()

        if self.use_per:
            (states, actions, rewards, next_states, dones), weights =\
                self.memory.sample(self.batch_size)
        else:
            states, actions, rewards, next_states, dones =\
                self.memory.sample(self.batch_size)
            weights = None

        # Calculate features of states.
        state_embeddings = self.online_net.calculate_state_embeddings(states)

        quantile_loss, mean_q, errors = self.calculate_loss(
            state_embeddings, actions, rewards, next_states, dones, weights)
        assert errors.shape == (self.batch_size, 1)
        
        mse_loss = self.calculate_mse_loss(states, actions, rewards, next_states, dones, weights)

        update_params(
            self.mean_optim, mse_loss,
            networks=[self.online_mean_net],
            retain_graph=False, grad_cliping=self.grad_cliping)
        
        update_params(
            self.optim, quantile_loss,
            networks=[self.online_net],
            retain_graph=False, grad_cliping=self.grad_cliping)

        if self.use_per:
            self.memory.update_priority(errors)

        if self.steps % self.log_interval == 0:
            self.writer.add_scalar('loss/quantile_loss', quantile_loss.detach().item(), self.steps)
            self.writer.add_scalar('loss/mse_loss', mse_loss.detach().item(), self.steps)
            self.writer.add_scalar('stats/mean_Q', mean_q, self.steps)

    def calculate_loss(self, state_embeddings, actions, rewards, next_states,
                       dones, weights):
        # Sample fractions.
        taus = torch.rand(
            self.batch_size, self.N, dtype=state_embeddings.dtype,
            device=state_embeddings.device)

        # Calculate quantile values of current states and actions at tau_hats.
        current_sa_quantiles = evaluate_quantile_at_action(
            self.online_net.calculate_quantiles(
                taus, state_embeddings=state_embeddings),
            actions)
        assert current_sa_quantiles.shape == (
            self.batch_size, self.N, 1)

        with torch.no_grad():
            # Calculate Q values of next states.
            if self.double_q_learning:
                # Sample the noise of online network to decorrelate between
                # the action selection and the quantile calculation.
                self.online_net.sample_noise()
                next_q = self.online_net.calculate_q(states=next_states)
            else:
                next_state_embeddings =\
                    self.target_net.calculate_state_embeddings(next_states)
                next_q = self.target_net.calculate_q(
                    state_embeddings=next_state_embeddings)

            # Calculate greedy actions.
            next_actions = torch.argmax(next_q, dim=1, keepdim=True)
            assert next_actions.shape == (self.batch_size, 1)

            # Calculate features of next states.
            if self.double_q_learning:
                next_state_embeddings =\
                    self.target_net.calculate_state_embeddings(next_states)

            # Sample next fractions.
            tau_dashes = torch.rand(
                self.batch_size, self.N_dash, dtype=state_embeddings.dtype,
                device=state_embeddings.device)

            # Calculate quantile values of next states and next actions.
            next_sa_quantiles = evaluate_quantile_at_action(
                self.target_net.calculate_quantiles(
                    tau_dashes, state_embeddings=next_state_embeddings
                ), next_actions).transpose(1, 2)
            assert next_sa_quantiles.shape == (self.batch_size, 1, self.N_dash)

            # Calculate target quantile values.
            target_sa_quantiles = rewards[..., None] + (
                1.0 - dones[..., None]) * self.gamma_n * next_sa_quantiles
            assert target_sa_quantiles.shape == (
                self.batch_size, 1, self.N_dash)

        td_errors = target_sa_quantiles - current_sa_quantiles
        assert td_errors.shape == (self.batch_size, self.N, self.N_dash)

        quantile_huber_loss = calculate_quantile_huber_loss(
            td_errors, taus, weights, self.kappa)

        return quantile_huber_loss, next_q.detach().mean().item(), \
            td_errors.detach().abs().sum(dim=1).mean(dim=1, keepdim=True)
            
    def calculate_mse_loss(self, states, actions, rewards, next_states, dones, weights):
        current_sa_quantiles = evaluate_quantile_at_action(self.online_mean_net(states=states),
                                                           actions)
        assert current_sa_quantiles.shape == (self.batch_size, 1, 1)
        
        with torch.no_grad():
            # Calculate Q values of next states.
            if self.double_q_learning:
                # Sample the noise of online network to decorrelate between
                # the action selection and the quantile calculation.
                self.online_net.sample_noise()
                next_q = self.online_mean_net.calculate_q(states=next_states)
            else:
                next_q = self.target_mean_net.calculate_q(states=next_states)

            # Calculate greedy actions.
            next_actions = torch.argmax(next_q, dim=1, keepdim=True)
            assert next_actions.shape == (self.batch_size, 1)

            # Calculate quantile values of next states and actions at tau_hats.
            next_sa_quantiles = evaluate_quantile_at_action(self.target_mean_net(states=next_states),
                                                            next_actions)
            assert next_sa_quantiles.shape == (self.batch_size, 1, 1)

            # Calculate target quantile values.
            target_sa_quantiles = rewards[..., None] + (1.0 - dones[..., None]) * self.gamma_n * next_sa_quantiles
            assert target_sa_quantiles.shape == (self.batch_size, 1, 1)
            
        mean_loss = F.mse_loss(current_sa_quantiles, target_sa_quantiles)
        return mean_loss
