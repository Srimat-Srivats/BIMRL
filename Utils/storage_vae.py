import numpy as np
import torch

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")


class RolloutStorageVAE(object):
    def __init__(self, num_processes, max_trajectory_len, zero_pad, max_num_rollouts,
                 state_dim, action_dim, vae_buffer_add_thresh, task_dim, save_intrinsic_reward=False):
        """
        Store everything that is needed for the VAE update
        :param num_processes:
        """

        self.obs_dim = state_dim
        self.action_dim = action_dim
        self.task_dim = task_dim
        self.save_intrinsic_reward = save_intrinsic_reward

        self.vae_buffer_add_thresh = vae_buffer_add_thresh  # prob of adding new trajectories
        self.max_buffer_size = max_num_rollouts  # maximum buffer len (number of trajectories)
        self.insert_idx = 0  # at which index we're currently inserting new data
        self.buffer_len = 0  # how much of the buffer has been filled

        # how long a trajectory can be at max (horizon)
        self.max_traj_len = max_trajectory_len
        # whether to zero-pad to maximum length (zero's at the end!)
        self.zero_pad = zero_pad

        # buffers for completed rollouts (stored on CPU)
        if self.max_buffer_size > 0:
            self.prev_state = torch.zeros((self.max_traj_len, self.max_buffer_size, state_dim))
            self.next_state = torch.zeros((self.max_traj_len, self.max_buffer_size, state_dim))
            self.actions = torch.zeros((self.max_traj_len, self.max_buffer_size, action_dim))
            self.rewards = torch.zeros((self.max_traj_len, self.max_buffer_size, 1))
            self.masks = torch.zeros((self.max_traj_len, self.max_buffer_size, 1))
            self.bad_masks = torch.zeros((self.max_traj_len, self.max_buffer_size, 1))
            self.done_task = torch.zeros((self.max_traj_len, self.max_buffer_size, 1))
            self.done_episode = torch.zeros((self.max_traj_len, self.max_buffer_size, 1))
            if self.save_intrinsic_reward:
                self.intrinsic_rewards = torch.zeros((self.max_traj_len, self.max_buffer_size, 1))
            if task_dim is not None:
                self.tasks = torch.zeros((self.max_buffer_size, task_dim))
            else:
                self.tasks = None
            self.trajectory_lens = [0] * self.max_buffer_size

        # storage for each running process (stored on GPU)
        self.num_processes = num_processes
        self.curr_timestep = torch.zeros((num_processes)).long()  # count environment steps so we know where to insert
        self.running_prev_state = torch.zeros((self.max_traj_len, num_processes, state_dim)).to(device)  # for each episode will have obs 0...N-1
        self.running_next_state = torch.zeros((self.max_traj_len, num_processes, state_dim)).to(device)  # for each episode will have obs 1...N
        self.running_rewards = torch.zeros((self.max_traj_len, num_processes, 1)).to(device)
        self.running_mask = torch.zeros((self.max_traj_len, num_processes, 1)).to(device)
        self.running_bad_masks = torch.zeros((self.max_traj_len, num_processes, 1)).to(device)
        if self.save_intrinsic_reward:
            self.running_intrinsic_rewards = torch.zeros((self.max_traj_len, num_processes, 1)).to(device)
        self.running_actions = torch.zeros((self.max_traj_len, num_processes, action_dim)).to(device)
        self.running_done_task = torch.zeros((self.max_traj_len, num_processes, 1)).to(device)
        self.running_done_episode = torch.zeros((self.max_traj_len, num_processes, 1)).to(device)
        if task_dim is not None:
            self.running_tasks = torch.zeros((num_processes, task_dim)).to(device)
        else:
            self.running_tasks = None

    def get_running_batch(self):
        """
        Returns the batch of data from the current running environments
        (zero-padded to maximal trajectory length since different processes can have different trajectory lengths)
        :return:
        """
        rewards = self.running_intrinsic_rewards if self.save_intrinsic_reward else self.running_rewards
        return self.running_prev_state, self.running_next_state, self.running_actions, rewards, self.curr_timestep

    def insert(self, prev_state, actions, next_state, rewards, done, task, masks, bad_masks, intrinsic_rewards, done_task, done_episode):

        # add to temporary buffer

        already_inserted = False
        if len(np.unique(self.curr_timestep)) == 1:
            self.running_prev_state[self.curr_timestep[0]] = prev_state
            self.running_next_state[self.curr_timestep[0]] = next_state
            self.running_rewards[self.curr_timestep[0]] = rewards
            self.running_mask[self.curr_timestep[0]] = masks
            self.running_bad_masks[self.curr_timestep[0]] = bad_masks
            if self.save_intrinsic_reward:
                self.running_intrinsic_rewards[self.curr_timestep[0]] = intrinsic_rewards
            self.running_actions[self.curr_timestep[0]] = actions
            self.running_done_task[self.curr_timestep[0]] = done_task
            self.running_done_episode[self.curr_timestep[0]] = done_episode
            if task is not None:
                self.running_tasks = task
            self.curr_timestep += 1
            already_inserted = True

        already_reset = False
        if done.sum() == self.num_processes:  # check if we can process the entire batch at once

            # add to permanent (up to max_buffer_len) buffer
            if self.max_buffer_size > 0:
                if self.vae_buffer_add_thresh >= np.random.uniform(0, 1):
                    # check where to insert data
                    if self.insert_idx + self.num_processes > self.max_buffer_size:
                        # keep track of how much we filled the buffer (for sampling from it)
                        self.buffer_len = self.insert_idx
                        # this will keep some entries at the end of the buffer without overwriting them,
                        # but the buffer is large enough to make this negligible
                        self.insert_idx = 0
                    else:
                        self.buffer_len = max(self.buffer_len, self.insert_idx)
                    # add; note: num trajectories are along dim=1,
                    # trajectory length along dim=0, to match pytorch RNN interface
                    self.prev_state[:, self.insert_idx:self.insert_idx + self.num_processes] = self.running_prev_state
                    self.next_state[:, self.insert_idx:self.insert_idx + self.num_processes] = self.running_next_state
                    self.actions[:, self.insert_idx:self.insert_idx+self.num_processes] = self.running_actions
                    self.done_task[:, self.insert_idx:self.insert_idx+self.num_processes] = self.running_done_task
                    self.done_episode[:, self.insert_idx:self.insert_idx + self.num_processes] = self.running_done_episode
                    self.rewards[:, self.insert_idx:self.insert_idx+self.num_processes] = self.running_rewards
                    self.masks[:, self.insert_idx:self.insert_idx+self.num_processes] = self.running_mask
                    self.bad_masks[:, self.insert_idx:self.insert_idx+self.num_processes] = self.running_bad_masks
                    if self.save_intrinsic_reward:
                        self.intrinsic_rewards[:, self.insert_idx:self.insert_idx+self.num_processes] = self.running_intrinsic_rewards
                    if (self.tasks is not None) and (self.running_tasks is not None):
                        insert_shape = self.tasks[self.insert_idx:self.insert_idx+self.num_processes].shape
                        self.tasks[self.insert_idx:self.insert_idx+self.num_processes] = self.running_tasks.reshape(insert_shape)
                    self.trajectory_lens[self.insert_idx:self.insert_idx+self.num_processes] = self.curr_timestep.clone()
                    self.insert_idx += self.num_processes

            # empty running buffer
            self.running_prev_state *= 0
            self.running_next_state *= 0
            self.running_rewards *= 0
            self.running_mask *= 0
            self.running_bad_masks *= 0
            if self.save_intrinsic_reward:
                self.running_intrinsic_rewards *= 0
            self.running_actions *= 0
            self.running_done_task *= 0
            self.running_done_episode *= 0
            if self.running_tasks is not None:
                self.running_tasks *= 0
            self.curr_timestep *= 0

            already_reset = True

        if (not already_inserted) or (not already_reset):

            for i in range(self.num_processes):

                if not already_inserted:
                    self.running_prev_state[self.curr_timestep[i], i] = prev_state[i]
                    self.running_next_state[self.curr_timestep[i], i] = next_state[i]
                    self.running_rewards[self.curr_timestep[i], i] = rewards[i]
                    self.running_mask[self.curr_timestep[i], i] = masks[i]
                    self.running_bad_masks[self.curr_timestep[i], i] = bad_masks[i]
                    if self.save_intrinsic_reward:
                        self.running_intrinsic_rewards[self.curr_timestep[i], i] = intrinsic_rewards[i]
                    self.running_actions[self.curr_timestep[i], i] = actions[i]
                    self.running_done_task[self.curr_timestep[i], i] = done_task[i]
                    self.running_done_episode[self.curr_timestep[i], i] = done_task[i]

                    if self.running_tasks[i] is None:
                        self.running_tasks[i] = task[i]
                    self.curr_timestep[i] += 1

                if not already_reset:
                    # if we are at the end of a task, dump the data into the larger buffer
                    if done[i]:

                        # add to permanent (up to max_buffer_len) buffer
                        if self.max_buffer_size > 0:
                            if self.vae_buffer_add_thresh >= np.random.uniform(0, 1):
                                # check where to insert data
                                if self.insert_idx + 1 > self.max_buffer_size:
                                    # keep track of how much we filled the buffer (for sampling from it)
                                    self.buffer_len = self.insert_idx
                                    # this will keep some entries at the end of the buffer without overwriting them,
                                    # but the buffer is large enough to make this negligible
                                    self.insert_idx = 0
                                else:
                                    self.buffer_len = max(self.buffer_len, self.insert_idx)
                                # add; note: num trajectories are along dim=1,
                                # trajectory length along dim=0, to match pytorch RNN interface
                                self.prev_state[:, self.insert_idx] = self.running_prev_state[:, i].to('cpu')
                                self.next_state[:, self.insert_idx] = self.running_next_state[:, i].to('cpu')
                                self.actions[:, self.insert_idx] = self.running_actions[:, i].to('cpu')
                                self.done_task[:, self.insert_idx] = self.running_done_task[:, i].to('cpu')
                                self.done_episode[:, self.insert_idx] = self.running_done_episode[:, i].to('cpu')
                                self.rewards[:, self.insert_idx] = self.running_rewards[:, i].to('cpu')
                                self.masks[:, self.insert_idx] = self.running_mask[:, i].to('cpu')
                                self.bad_masks[:, self.insert_idx] = self.running_bad_masks[:, i].to('cpu')
                                if self.save_intrinsic_reward:
                                    self.intrinsic_rewards[:, self.insert_idx] = self.running_intrinsic_rewards[:, i].to('cpu')
                                if self.tasks is not None:
                                    self.tasks[self.insert_idx] = self.running_tasks[i].to('cpu')
                                self.trajectory_lens[self.insert_idx] = self.curr_timestep[i].clone()
                                self.insert_idx += 1

                        # empty running buffer
                        self.running_prev_state[:, i] *= 0
                        self.running_next_state[:, i] *= 0
                        self.running_rewards[:, i] *= 0
                        self.running_mask[:, i] *= 0
                        self.running_bad_masks[:, i] *= 0
                        if self.save_intrinsic_reward:
                            self.running_intrinsic_rewards[:, i] *= 0
                        self.running_actions[:, i] *= 0
                        self.running_done_task[:, i] *= 0
                        self.running_done_episode[:, i] *= 0
                        if self.running_tasks is not None:
                            self.running_tasks[i] *= 0
                        self.curr_timestep[i] = 0

    def ready_for_update(self):
        return len(self) > 0

    def __len__(self):
        return self.buffer_len

    def get_batch(self, batchsize=5, replace=False, value_prediction=False, memory_batch=False):

        batchsize = min(self.buffer_len, batchsize)

        # select the indices for the processes from which we pick
        rollout_indices = np.random.choice(range(self.buffer_len), batchsize, replace=replace)
        # trajectory length of the individual rollouts we picked
        trajectory_lens = np.array(self.trajectory_lens)[rollout_indices]

        # select the rollouts we want
        prev_obs = self.prev_state[:, rollout_indices, :]
        next_obs = self.next_state[:, rollout_indices, :]
        actions = self.actions[:, rollout_indices, :]
        rewards = self.rewards[:, rollout_indices, :]
        if self.save_intrinsic_reward and value_prediction:
            rewards = self.intrinsic_rewards[:, rollout_indices, :]
        if self.tasks is not None:
            tasks = self.tasks[rollout_indices].to(device)
        else:
            tasks = None
        if value_prediction:
            masks = self.masks[:, rollout_indices, :]
            masks = torch.cat((torch.zeros(size=(1, batchsize, 1)), masks), dim=0)
            bad_masks = self.bad_masks[:, rollout_indices, :]
            bad_masks = torch.cat((torch.zeros(size=(1, batchsize, 1)), bad_masks), dim=0)
            return prev_obs.to(device), next_obs.to(device), actions.to(device), \
            rewards.to(device), tasks, masks.to(device), bad_masks.to(device), trajectory_lens
        if memory_batch:
            done_tasks = self.done_task[:, rollout_indices, :]
            done_episodes = self.done_episode[:, rollout_indices, :]
            return prev_obs.to(device), next_obs.to(device), actions.to(device), rewards.to(device), tasks.to(device), done_tasks.to(device),\
            done_episodes.to(device), trajectory_lens
        return prev_obs.to(device), next_obs.to(device), actions.to(device), \
               rewards.to(device), tasks, trajectory_lens