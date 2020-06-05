import os
import torch
from torch.optim import Adam

from .sac import SAC
from discor.network import TwinnedStateActionFunction
from discor.utils import disable_gradients, soft_update, update_params


class DisCor(SAC):

    def __init__(self, state_dim, action_dim, device, policy_lr=0.0003,
                 q_lr=0.0003, entropy_lr=0.0003, error_lr=0.0003,
                 policy_hidden_units=[256, 256], q_hidden_units=[256, 256],
                 error_hidden_units=[256, 256, 256],
                 tau_init=10.0, target_update_coef=0.005,
                 log_interval=10, seed=0):
        super().__init__(
            state_dim, action_dim, device, policy_lr, q_lr, entropy_lr,
            policy_hidden_units, q_hidden_units, target_update_coef,
            log_interval, seed)

        # Build error networks.
        self._online_error_net = TwinnedStateActionFunction(
            state_dim=state_dim,
            action_dim=action_dim,
            hidden_units=error_hidden_units
            ).to(device=self._device)
        self._target_error_net = TwinnedStateActionFunction(
            state_dim=state_dim,
            action_dim=action_dim,
            hidden_units=error_hidden_units
            ).to(device=self._device).eval()

        # Copy parameters of the learning network to the target network.
        self._target_error_net.load_state_dict(
            self._online_error_net.state_dict())

        # Disable gradient calculations of the target network.
        disable_gradients(self._target_error_net)

        self._error_optim = Adam(
            self._online_error_net.parameters(), lr=error_lr)

        self._tau1 = torch.tensor(tau_init, requires_grad=False)
        self._tau2 = torch.tensor(tau_init, requires_grad=False)

    def update_target(self):
        super().update_target()
        soft_update(
            self._target_error_net, self._online_error_net,
            self._target_update_coef)

    def learn(self, batch, writer):
        self._learning_steps += 1
        states, actions, rewards, next_states, dones = batch

        # Calculate current and target Q values.
        curr_qs1, curr_qs2 = self.calc_current_q(states, actions)
        target_qs = self.calc_target_q(rewards, next_states, dones)

        # Calculate current and target errors, as well as importance weights.
        curr_errs1, curr_errs2, imp_ws1, imp_ws2 = \
            self.calc_current_error(states, actions)
        target_errs1, target_errs2 = self.calc_target_error(
            next_states, dones, curr_qs1, curr_qs2, target_qs)

        # Update policy.
        policy_loss, entropies = self.calc_policy_loss(states)
        update_params(self._policy_optim, policy_loss)

        # Update Q functions.
        q_loss, mean_q1, mean_q2 = \
            self.calc_q_loss(curr_qs1, curr_qs2, target_qs, imp_ws1, imp_ws2)
        update_params(self._q_optim, q_loss)

        # Update the entropy coefficient.
        entropy_loss = self.calc_entropy_loss(entropies)
        update_params(self._alpha_optim, entropy_loss)
        self._alpha = self._log_alpha.exp()

        # Update error models.
        err_loss = self.calc_error_loss(
            curr_errs1, curr_errs2, target_errs1, target_errs2)
        update_params(self._error_optim, err_loss)

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
                'loss/error', err_loss.detach().item(),
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
            writer.add_scalar(
                'stats/tau1', self._tau1.item(), self._learning_steps)
            writer.add_scalar(
                'stats/tau2', self._tau2.item(), self._learning_steps)

    def calc_current_error(self, states, actions):
        curr_errs1, curr_errs2 = self._online_error_net(states, actions)

        # We calculate self-normalized importance weights.
        imp_ws1 = torch.exp(- self._gamma * curr_errs1.detach() / self._tau1)
        imp_ws2 = torch.exp(- self._gamma * curr_errs2.detach() / self._tau2)
        imp_ws1 = imp_ws1 / imp_ws1.sum()
        imp_ws2 = imp_ws2 / imp_ws2.sum()

        return curr_errs1, curr_errs2, imp_ws1, imp_ws2

    def calc_target_error(self, next_states, dones, curr_qs1, curr_qs2,
                          target_qs):
        # Calculate targets of the cumulative sum of discounted Bellman errors,
        # which is 'Delta' in the paper.
        with torch.no_grad():
            next_actions, next_entropies, _ = self._policy_net(next_states)
            next_errs1, next_errs2 = \
                self._target_error_net(next_states, next_actions)

            target_errs1 = (curr_qs1 - target_qs).abs() + \
                (1.0 - dones) * self._gamma * next_errs1
            target_errs2 = (curr_qs2 - target_qs).abs() + \
                (1.0 - dones) * self._gamma * next_errs2

        return target_errs1, target_errs2

    def calc_error_loss(self, curr_errs1, curr_errs2, target_errs1,
                        target_errs2):
        err1_loss = torch.mean((curr_errs1 - target_errs1).pow(2))
        err2_loss = torch.mean((curr_errs2 - target_errs2).pow(2))

        self._tau1 = self._tau1 * (1.0 - self._target_update_coef) + \
            curr_errs1.detach().mean() * self._target_update_coef
        self._tau2 = self._tau2 * (1.0 - self._target_update_coef) + \
            curr_errs2.detach().mean() * self._target_update_coef

        return err1_loss + err2_loss

    def save_models(self, save_dir):
        super().save_models(save_dir)
        self._online_error_net.save(
            os.path.join(save_dir, 'online_error_net.pth'))
        self._target_error_net.save(
            os.path.join(save_dir, 'target_error_net.pth'))
