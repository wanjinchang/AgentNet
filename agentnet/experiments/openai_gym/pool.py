"""
A thin wrapper for openAI gym environments that maintains a set of parallel games and has a method to generate interaction sessions
given agent one-step applier function
"""

import gym
import numpy as np
from ...utils.layers import get_layer_dtype
from ...environment import SessionPoolEnvironment
function = type(lambda:0)

def GamePool(*args,**kwargs):
    raise ValueError("Deprecated. Use EnvPool(agent,env_title,n_parallel_agents) instead")

# A whole lot of space invaders
class EnvPool(object):
    def __init__(self, agent, make_env=lambda:gym.make("SpaceInvaders-v0"), n_games=1, max_size=None):
        """
        A pool that stores several
           - game states (gym environment)
           - prev_observations - last agent observations
           - prev memory states - last agent hidden states

        and is capable of some auxilary actions like evaluating agent on one game session.

        :param make_env: a factory that produces environments OR a name of the gym environment.
                See gym.envs.registry.all()
        :param n_games: number of parallel games
        :param max_size: max pool size by default (if appending sessions)
        """
        if not isinstance(make_env, function):
            env_name = make_env
            make_env = lambda: gym.make(env_name)

        #create atari games
        self.make_env = make_env
        self.envs = [self.make_env() for _ in range(n_games)]

        #initial observations
        self.prev_observations = [make_env.reset() for make_env in self.envs]

        #agent memory variables (if you use recurrent networks
        self.prev_memory_states = [np.zeros((n_games,)+tuple(mem.output_shape[1:]),
                                   dtype=get_layer_dtype(mem))
                         for mem in agent.agent_states]

        #save agent
        self.agent = agent
        self.agent_step = agent.get_react_function()

        # Create experience replay environment
        self.experience_replay = SessionPoolEnvironment(observations=agent.observation_layers,
                                                        actions=agent.action_layers,
                                                        agent_memories=agent.agent_states)
        self.max_size = max_size



    def interact(self, n_steps=100, verbose=False):
        """generate interaction sessions with ataries (openAI gym atari environments)
        Sessions will have length n_steps.
        Each time one of games is finished, it is immediately getting reset


        params:
            agent_step: a function(observations,memory_states) -> actions,new memory states for agent update
            n_steps: length of an interaction
            verbose: if True, prints small debug message whenever a game gets reloaded after end.
        returns:
            observation_log,action_log,reward_log,[memory_logs],is_alive_log,info_log
            a bunch of tensors [batch, tick, size...]

            the only exception is info_log, which is a list of infos for [time][batch]
        """
        history_log = []
        for i in range(n_steps):
            res = self.agent_step(self.prev_observations, *self.prev_memory_states)
            actions, new_memory_states = res[0],res[1:]

            new_observations, cur_rewards, is_done, infos = \
                zip(*map(
                    lambda env, action: env.step(action),
                    self.envs,
                    actions)
                    )

            new_observations = np.array(new_observations)

            for i in range(len(self.envs)):
                if is_done[i]:
                    new_observations[i] = self.envs[i].reset()

                    for m_i in range(len(new_memory_states)):
                        new_memory_states[m_i][i] = 0

                    if verbose:
                        print("env %i reloaded" % i)

            # append observation -> action -> reward tuple
            history_log.append((self.prev_observations, actions, cur_rewards, new_memory_states, is_done, infos))

            self.prev_observations = new_observations
            self.prev_memory_states = new_memory_states

        # cast to numpy arrays
        observation_log, action_log, reward_log, memories_log, is_done_log, info_log = zip(*history_log)

        # tensor dimensions
        # [batch_i, time_i, observation_size...]
        observation_log = np.array(observation_log).swapaxes(0, 1)

        # [batch, time, units] for each memory tensor
        memories_log = map(lambda mem: np.array(mem).swapaxes(0, 1), zip(*memories_log))

        # [batch_i,time_i]
        action_log = np.array(action_log).swapaxes(0, 1)

        # [batch_i, time_i]
        reward_log = np.array(reward_log).swapaxes(0, 1)

        # [batch_i, time_i]
        is_alive_log = 1 - np.array(is_done_log, dtype='int8').swapaxes(0, 1)


        return observation_log, action_log, reward_log, memories_log, is_alive_log, info_log


    def update(self,n_steps=100,append=False,max_size=None):
        """ a function that creates new sessions and ads them into the pool
        throwing the old ones away entirely for simplicity"""

        preceding_memory_states = list(self.prev_memory_states)

        # get interaction sessions
        observation_tensor, action_tensor, reward_tensor, _, is_alive_tensor, _ = self.interact(n_steps=n_steps)

        # load them into experience replay environment
        if not append:
            self.experience_replay.load_sessions(observation_tensor, action_tensor, reward_tensor,
                                                 is_alive_tensor, preceding_memory_states)
        else:
            self.experience_replay.append_sessions(observation_tensor, action_tensor, reward_tensor,
                                                 is_alive_tensor, preceding_memory_states,
                                                   max_pool_size=max_size or self.max_size)


    def evaluate(self,n_games=1,save_path="./records", record_video=True,verbose=True,t_max=10000):
        """
        Plays an entire game start to end, records the logs(and possibly mp4 video), returns reward
        :param save_path: where to save the report
        :param record_video: if True, records mp4 video
        :return: total reward (scalar)
        """
        env = self.make_env()

        if record_video:
            env.monitor.start(save_path, force=True)
        else:
            env.monitor.start(save_path, lambda i: False, force=True)

        game_rewards = []
        for _ in range(n_games):
            # initial observation
            observation = env.reset()
            # initial memory
            prev_memories = [np.zeros((1,) + tuple(mem.output_shape[1:]),
                                      dtype=get_layer_dtype(mem))
                             for mem in self.agent.agent_states]

            t = 0
            total_reward = 0
            while True:

                res = self.agent_step(observation[None,...], *prev_memories)
                action, new_memories = res[0],res[1:]

                observation, reward, done, info = env.step(action[0])
                total_reward += reward
                prev_memories = new_memories

                if done or t >= t_max:
                    if verbose:
                        print("Episode finished after {} timesteps with reward={}".format(t + 1,total_reward))
                    break
                t += 1
            game_rewards.append(total_reward)

        env.monitor.close()
        del env
        return game_rewards