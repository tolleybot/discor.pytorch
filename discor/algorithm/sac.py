import os
import torch
from torch.optim import Adam

from .base import Algorithm
from discor.network import TwinnedStateActionFunction, GaussianPolicy
from discor.utils import disable_gradients, soft_update, update_params


class SAC(Algorithm):

    def __init__(self, state_dim, action_dim, device, policy_lr=0.0003,
                 q_lr=0.0003, entropy_lr=0.0003,
                 policy_hidden_units=[256, 256], q_hidden_units=[256, 256],
                 target_update_coef=0.005, log_interval=10, seed=0):
        super().__init__(device, log_interval, seed)

        # Build networks.
        self._policy_net = GaussianPolicy(
            state_dim=state_dim,
            action_dim=action_dim,
            hidden_units=policy_hidden_units
            ).to(self._device)
        self._online_q_net = TwinnedStateActionFunction(
            state_dim=state_dim,
            action_dim=action_dim,
            hidden_units=q_hidden_units
            ).to(self._device)
        self._target_q_net = TwinnedStateActionFunction(
            state_dim=state_dim,
            action_dim=action_dim,
            hidden_units=q_hidden_units
            ).to(self._device).eval()

        # Copy parameters of the learning network to the target network.
        self._target_q_net.load_state_dict(self._online_q_net.state_dict())

        # Disable gradient calculations of the target network.
        disable_gradients(self._target_q_net)

        # Optimizers.
        self._policy_optim = Adam(self._policy_net.parameters(), lr=policy_lr)
        self._q_optim = Adam(self._online_q_net.parameters(), lr=q_lr)

        # Target entropy is -|A|.
        self._target_entropy = -float(action_dim)

        # We optimize log(alpha), instead of alpha.
        self._log_alpha = torch.zeros(
            1, device=self._device, requires_grad=True)
        self._alpha = self._log_alpha.exp()
        self._alpha_optim = Adam([self._log_alpha], lr=entropy_lr)

        self._target_update_coef = target_update_coef

    def explore(self, state):
        state = torch.tensor(
            state[None, ...], dtype=torch.float, device=self._device)
        with torch.no_grad():
            action, _, _ = self._policy_net(state)
        return action.cpu().numpy()[0]

    def exploit(self, state):
        state = torch.tensor(
            state[None, ...], dtype=torch.float, device=self._device)
        with torch.no_grad():
            _, _, action = self._policy_net(state)
        return action.cpu().numpy()[0]

    def update_target_networks(self):
        soft_update(
            self._target_q_net, self._online_q_net, self._target_update_coef)

    def learn(self, batch, writer):
        self._learning_steps += 1
        states, actions, rewards, next_states, dones = batch

        # Calculate current and target Q values.
        curr_qs1, curr_qs2 = self.calc_current_q(states, actions)
        target_qs = self.calc_target_q(rewards, next_states, dones)

        # Update policy.
        policy_loss, entropies = self.calc_policy_loss(states)
        update_params(self._policy_optim, policy_loss)

        # Update Q functions.
        q_loss, mean_q1, mean_q2 = \
            self.calc_q_loss(curr_qs1, curr_qs2, target_qs)
        update_params(self._q_optim, q_loss)

        # Update the entropy coefficient.
        entropy_loss = self.calc_entropy_loss(entropies)
        update_params(self._alpha_optim, entropy_loss)
        self._alpha = self._log_alpha.exp()

        if self._learning_steps % self._log_interval == 0:
            writer.add_scalar(
                'loss/policy', policy_loss.detach().item(),
                self._learning_steps)
            writer.add_scalar(
                'loss/Q', q_loss.detach().item(),
                self._learning_steps)
            writer.add_scalar(
                'loss/entropy', entropy_loss.detach().item(),
                self._learning_steps)
            writer.add_scalar(
                'stats/alpha', self._alpha.detach().item(),
                self._learning_steps)
            writer.add_scalar(
                'stats/mean_Q1', mean_q1, self._learning_steps)
            writer.add_scalar(
                'stats/mean_Q2', mean_q2, self._learning_steps)
            writer.add_scalar(
                'stats/entropy', entropies.detach().mean().item(),
                self._learning_steps)

    def calc_current_q(self, states, actions):
        curr_qs1, curr_qs2 = self._online_q_net(states, actions)
        return curr_qs1, curr_qs2

    def calc_target_q(self, rewards, next_states, dones):
        with torch.no_grad():
            next_actions, next_entropies, _ = self._policy_net(next_states)
            next_qs1, next_qs2 = self._target_q_net(next_states, next_actions)
            next_qs = \
                torch.min(next_qs1, next_qs2) + self._alpha * next_entropies

        assert rewards.shape == next_qs.shape
        target_qs = rewards + (1.0 - dones) * self._discount * next_qs

        return target_qs

    def calc_q_loss(self, curr_qs1, curr_qs2, target_qs, imp_ws1=1.0,
                    imp_ws2=1.0):
        assert isinstance(imp_ws1, float) or imp_ws1.shape == curr_qs1.shape
        assert isinstance(imp_ws2, float) or imp_ws2.shape == curr_qs2.shape
        assert not target_qs.requires_grad
        assert curr_qs1.shape == target_qs.shape

        # Q loss is mean squared TD errors with importance weights.
        q1_loss = torch.mean((curr_qs1 - target_qs).pow(2) * imp_ws1)
        q2_loss = torch.mean((curr_qs2 - target_qs).pow(2) * imp_ws2)

        # Mean Q values for logging.
        mean_q1 = curr_qs1.detach().mean().item()
        mean_q2 = curr_qs2.detach().mean().item()

        return q1_loss + q2_loss, mean_q1, mean_q2

    def calc_policy_loss(self, states):
        # Resample actions to calculate expectations of Q.
        sampled_actions, entropies, _ = self._policy_net(states)

        # Expectations of Q with clipped double Q technique.
        qs1, qs2 = self._online_q_net(states, sampled_actions)
        qs = torch.min(qs1, qs2)

        # Policy objective is maximization of (Q + alpha * entropy).
        assert qs.shape == entropies.shape
        policy_loss = torch.mean((- qs - self._alpha * entropies))

        return policy_loss, entropies.detach()

    def calc_entropy_loss(self, entropies):
        assert not entropies.requires_grad

        # Intuitively, we increse alpha when entropy is less than target
        # entropy, vice versa.
        entropy_loss = -torch.mean(
            self._log_alpha * (self._target_entropy - entropies))
        return entropy_loss

    def save_models(self, save_dir):
        super().save_models(save_dir)
        self._policy_net.save(os.path.join(save_dir, 'policy_net.pth'))
        self._online_q_net.save(os.path.join(save_dir, 'online_q_net.pth'))
        self._target_q_net.save(os.path.join(save_dir, 'target_q_net.pth'))
