"""
Basic Q-learning implementation
"""
from __future__ import division, print_function, absolute_import

import theano.tensor as T
from lasagne.objectives import squared_error

from .helpers import get_end_indicator, get_action_Qvalues
from ..utils.grad import consider_constant


def get_reference_Qvalues(Qvalues, actions, rewards,
                          gamma_or_gammas=0.95,
                          qvalues_after_end="zeros",
                          aggregation_function=lambda qv: T.max(qv, axis=-1)
                          ):
    """
    Returns reference Q-values according to Q-learning algorithm

        Qreference(state,action) = reward(state,action) + gamma* max[next_action]( Q(next_state,next_action)

    :param Qvalues: [batch,tick,action_id] - predicted qvalues
    :param actions: [batch,tick] - commited actions
    :param rewards: [batch,tick] - immediate rewards for taking actions at given time ticks
    :param gamma_or_gammas:  a single value or array[batch,tick](can broadcast dimensions) of delayed reward discounts
    :param qvalues_after_end: [batch,n_actions] - symbolic expression for "next state q-values" for last tick used for reference only.
                            Defaults at  T.zeros_like(Qvalues[:,0,None,:])
                            If you wish to simply ignore the last tick, use defaults and crop output's last tick ( qref[:,:-1] )
    :param aggregation_function: a function that takes all Q-values for "next state qvalues" term and returns what
                                is the "best next Qvalue". Normally you should not touch it. Defaults to max over actions.
                                Normaly you shouldn't touch this.
                                Takes input of [batch,tick,n_actions] Q-values
    :return: Qreference - reference qvalues at [batch,tick] using formula above

    Q reference [batch,action_at_tick_t] = rewards[t] + gamma_or_gammas* Qs(t+1, best_action_at(t+1))

                where  Qs(t+1, best_action_at(t+1)) is computed as aggregation_function(next_Qvalues)

    """
    if qvalues_after_end == "zeros":
        qvalues_after_end = T.zeros_like(Qvalues[:, 0, None, :])

    # Q-values for "next" states (padded with zeros at the end): float[batch,tick,action]
    next_Qvalues_predicted = T.concatenate(
        [
            Qvalues[:, 1:],
            qvalues_after_end,
        ],
        axis=1
    )

    # "optimal next reward" after commiting action : float[batch,tick]
    optimal_next_Qvalue = aggregation_function(next_Qvalues_predicted)

    # full Qvalue formula (taking chosen_action and behaving optimally later on)
    reference_Qvalues = rewards + gamma_or_gammas * optimal_next_Qvalue

    return reference_Qvalues


def get_elementwise_objective(Qvalues, actions, rewards,
                              is_alive="always",
                              Qvalues_target=None,
                              gamma_or_gammas=0.95,
                              crop_last = True,
                              force_qvalues_after_end=True,
                              qvalues_after_end="zeros",
                              consider_reference_constant=True,
                              aggregation_function=lambda qv: T.max(qv, axis=2)):
    """
    Returns squared error between predicted and reference Qvalues according to Q-learning algorithm

        Qreference(state,action) = reward(state,action) + gamma* max[next_action]( Q(next_state,next_action)
        loss = mean over (Qvalues - Qreference)**2

    :param Qvalues: [batch,tick,action_id] - predicted qvalues
    :param actions: [batch,tick] - commited actions
    :param rewards: [batch,tick] - immediate rewards for taking actions at given time ticks
    :param is_alive: [batch,tick] - whether given session is still active at given tick. Defaults to always active.
                            Default value of is_alive implies a simplified computation algorithm for Qlearning loss
    :param Qvalues_target: Older snapshot Qvalues (e.g. from a target network). If None, uses current Qvalues
    :param gamma_or_gammas:  a single value or array[batch,tick](can broadcast dimensions) of delayed reward discounts
    :param qvalues_after_end: [batch,n_actions] - symbolic expression for "next state q-values" for last tick used for reference only.
                            Defaults at  T.zeros_like(Qvalues[:,0,None,:])
                            If you wish to simply ignore the last tick, use defaults and crop output's last tick ( qref[:,:-1] )
    :param aggregation_function: a function that takes all Q-values for "next state qvalues" term and returns what
                                is the "best next Qvalue". Normally you should not touch it. Defaults to max over actions.
                                Normaly you shouldn't touch this.
                                Takes input of [batch,tick,n_actions] Q-values

    :param crop_last: if True, zeros-out loss at final tick, if False - computes loss VS Qvalues_after_end
    :param force_qvalues_after_end:  if true, sets reference Qvalues at session end to rewards[end] + qvalues_after_end
    :param qvalues_after_end:
    :param consider_reference_constant: whether or not zero-out gradient flow through reference_Qvalues (True highly recommended)
    :param aggregation_function:
    :return: tensor [batch, tick] of squared errors over Q-values (using formua above for loss)
                    If Qvalues_target are provided, they are used for reference computation instead of original Qvalues

    """
    if Qvalues_target is None:
        Qvalues_target = Qvalues

    # get reference Q-values via Q-learning algorithm
    reference_Qvalues = get_reference_Qvalues(Qvalues_target, actions, rewards,
                                              gamma_or_gammas=gamma_or_gammas,
                                              qvalues_after_end=qvalues_after_end,
                                              aggregation_function=aggregation_function
                                              )

    if consider_reference_constant:
        # do not pass gradient through reference Q-values (since they DO depend on Q-values by default)
        reference_Qvalues = consider_constant(reference_Qvalues)

    # get predicted Q-values for committed actions (to compare with reference Q-values)
    action_Qvalues = get_action_Qvalues(Qvalues, actions)

    # if agent is always alive, return the simplified loss
    if is_alive == "always":

        # tensor of element-wise squared errors
        elwise_squared_error = squared_error(reference_Qvalues, action_Qvalues)

    else:
        # we are given an is_alive matrix : uint8[batch,tick]

        # if asked to force reference_Q[end_tick+1,a] = 0, do it
        # note: if agent is always alive, this is meaningless

        if force_qvalues_after_end:
            # set future rewards at session end to rewards+qvalues_after_end
            end_ids = get_end_indicator(is_alive, force_end_at_t_max=True).nonzero()

            if qvalues_after_end == "zeros":
                # "set reference Q-values at end action ids to just the immediate rewards"
                reference_Qvalues = T.set_subtensor(reference_Qvalues[end_ids],
                                                    rewards[end_ids]
                                                    )
            else:
                last_optimal_rewards = aggregation_function(qvalues_after_end)

                # "set reference Q-values at end action ids to the immediate rewards + qvalues after end"
                reference_Qvalues = T.set_subtensor(reference_Qvalues[end_ids],
                                                    rewards[end_ids] + gamma_or_gammas * last_optimal_rewards[
                                                        end_ids[0], 0]
                                                    )

        # tensor of elementwise squared errors
        elwise_squared_error = squared_error(reference_Qvalues, action_Qvalues)

        # zero-out loss after session ended
        elwise_squared_error = elwise_squared_error * is_alive

    if crop_last:
        elwise_squared_error = T.set_subtensor(elwise_squared_error[:,-1],0)

    return elwise_squared_error
