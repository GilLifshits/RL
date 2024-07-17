import time

import tensorflow as tf
from RL.config import LEARNING_RATE, CAR1_NAME, CAR2_NAME, COLLISION_REWARD, REACHED_TARGET_REWARD, STARVATION_REWARD, \
    NOT_KEEPING_SAFETY_DISTANCE_REWARD, KEEPING_SAFETY_DISTANCE_REWARD, SAFETY_DISTANCE_FOR_PUNISH, \
    SAFETY_DISTANCE_FOR_BONUS, EPSILON_DECAY, LOG_ACTIONS_SELECTED, LOG_Q_VALUES, LOG_WEIGHTS_ARE_IDENTICAL, \
    TIME_BETWEEN_STEPS
from RL.utils.NN_utils import *
import numpy as np


class RL:

    def __init__(self, logger, airsim):
        self.logger = logger
        self.airsim = airsim
        self.optimizer = tf.keras.optimizers.legacy.Adam(learning_rate=LEARNING_RATE)
        self.discount_factor = 0.95
        self.epsilon = 0.9
        self.epsilon_decay = EPSILON_DECAY
        self.network = init_network_master_and_agent(self.optimizer) # TODO: change name network and network_car2
        self.network_car2 = create_network_copy(self.network)  # TODO: change name network and network_car2
        self.current_trajectory = []
        self.trajectories = []
        # self.batch_size = BATCH_SIZE_FOR_TRAJECTORY_BATCH  # relevant for train_batch_of_trajectories function
        self.freeze_master = False

    def step(self):
        """
        Main Idea: get current state, sample action, get next state, get reward, detect collision_occurred or reached_target
        returns: current_state, cars_actions, next_state, collision_occurred, reached_target, reward
        """

        # get current state
        car1_state = self.airsim.get_car1_state(self.logger)
        car2_state = self.airsim.get_car2_state(self.logger)

        # sample actions
        car1_action, car2_action = self.sample_action(car1_state, car2_state)

        # set updated controls according to sampled action
        self.set_controls_according_to_sampled_action(CAR1_NAME, car1_action)
        self.set_controls_according_to_sampled_action(CAR2_NAME, car2_action)

        # delay code in order to physically get the next state in the simulator
        time.sleep(TIME_BETWEEN_STEPS)

        # get next state
        car1_next_state = self.airsim.get_car1_state(self.logger)
        car2_next_state = self.airsim.get_car2_state(self.logger)

        # calculate reward
        collision_occurred = self.airsim.collision_occurred()
        reached_target = self.airsim.has_reached_target(car1_next_state)
        reward = self.calculate_reward(car1_next_state, collision_occurred, reached_target, car1_action, car2_action)

        # Put together master input
        master_input = [car1_state, car2_state]
        master_input_of_next_state = [car1_next_state, car2_next_state]

        # organize output
        current_state = [[master_input, car1_state], [master_input, car2_state]]
        cars_actions = [car1_action, car2_action]
        next_state = [[master_input_of_next_state, car1_next_state], [master_input_of_next_state, car2_next_state]]

        return current_state, cars_actions, next_state, collision_occurred, reached_target, reward

    def train_trajectory(self, train_only_last_step, episode_counter):
        """
        train_only_last_step = True -> train on the last 2 items (these are the items that describe the last step)
        train_only_last_step = False -> train on whole trajectory
        """

        # TODO: go over this function, compare with commented code
        # TODO: check that it works the same as code before.. compare the graients? or loss?

        if train_only_last_step:
            current_trajectory = self.current_trajectory[-2:]
        else:
            current_trajectory = self.current_trajectory

        states, actions, next_states, rewards = self.process_trajectory(current_trajectory)

        current_state_q_values = self.predict_q_values_of_trajectory(states)
        next_state_q_values = self.predict_q_values_of_trajectory(next_states)

        updated_q_values = self.update_q_values(actions, rewards, current_state_q_values, next_state_q_values)

        with tf.GradientTape(persistent=True) as tape:
            loss, gradients = self.apply_gradients(tape, self.prepare_state_inputs(states, separate_state_for_each_car=False), updated_q_values)
        self.logger.log_weights_and_gradients(gradients, episode_counter, self.network)

        return loss.numpy().mean()

    @staticmethod
    def process_trajectory(trajectory):
        """ Convert the trajectory into separate lists for each component. """
        states, actions, next_states, rewards = zip(*trajectory)
        return states, actions, next_states, rewards

    @staticmethod
    def prepare_state_inputs(states, separate_state_for_each_car): # -> Tuple[List[np.ndarray, np.ndarray], List[np.ndarray, np.ndarray]]
        """ Assemble the master and agent inputs from states. """
        if separate_state_for_each_car:
            states_car1 = states[::2]
            states_car2 = states[1::2]
            master_inputs_car1 = np.array([np.concatenate(state[0]) for state in states_car1])
            master_inputs_car2 = np.array([np.concatenate(state[0]) for state in states_car2])
            agent_inputs_car1 = np.array([state[1] for state in states_car1])
            agent_inputs_car2 = np.array([state[1] for state in states_car2])
            return [master_inputs_car1, agent_inputs_car1], [master_inputs_car2, agent_inputs_car2]
        else:
            master_inputs = np.array([np.concatenate(state[0]) for state in states])
            agent_inputs = np.array([state[1] for state in states])
            return [master_inputs, agent_inputs]

    def predict_q_values_of_trajectory(self, states):
        """ Predict Q-values for the given states (according to the network of each car) """
        car1_inputs, car2_inputs = self.prepare_state_inputs(states, separate_state_for_each_car=True)

        car1_q_values_of_trajectory = self.network.predict(car1_inputs, verbose=0)
        car2_q_values_of_trajectory = self.network.predict(car2_inputs, verbose=0)  # TODO: change self.network to self.netowkr_car2

        q_values_of_trajectory = np.empty((car1_q_values_of_trajectory.shape[0] + car2_q_values_of_trajectory.shape[0],
                                           car1_q_values_of_trajectory.shape[1]))
        q_values_of_trajectory[::2] = car1_q_values_of_trajectory
        q_values_of_trajectory[1::2] = car2_q_values_of_trajectory

        return q_values_of_trajectory

    def update_q_values(self, actions, rewards, current_q_values, next_q_values):
        """ Update Q-values using the DQN update rule for each step in the trajectory. """
        # Calculate max Q-value for the next state
        max_next_q_values = tf.reduce_max(next_q_values, axis=1)
        targets = rewards + self.discount_factor * max_next_q_values

        # Gather the Q-values corresponding to the taken actions
        indices = tf.stack([tf.range(len(actions)), actions], axis=1)
        gathered_q_values = tf.gather_nd(current_q_values, indices)

        # Update Q-values using the DQN update rule
        updated_q_values = gathered_q_values + LEARNING_RATE * (targets - gathered_q_values)

        # Update the current Q-values tensor with the updated values
        updated_q_values_tensor = tf.tensor_scatter_nd_update(current_q_values, indices, updated_q_values)
        return updated_q_values_tensor

    def apply_gradients(self, tape, current_state, updated_q_values):
        """ Calculate and apply gradients to the network. """
        current_state_q_values = self.network(current_state, training=True)
        loss = tf.keras.losses.mean_squared_error(updated_q_values, current_state_q_values)
        gradients = tape.gradient(loss, self.network.trainable_variables)
        self.network.optimizer.apply_gradients(zip(gradients, self.network.trainable_variables))
        return loss, gradients

    def sample_action(self, car1_state, car2_state):

        if np.random.binomial(1, p=self.epsilon):  # epsilon greedy
            if LOG_ACTIONS_SELECTED:
                self.logger.log_actions_selected_random()
            car1_random_action = np.random.randint(2, size=(1, 1))[0][0]
            car2_random_action = np.random.randint(2, size=(1, 1))[0][0]
            return car1_random_action, car2_random_action
        else:
            master_input = np.concatenate((car1_state, car2_state), axis=0).reshape(1, -1)
            car1_state = np.reshape(car1_state, (1, -1))
            car2_state = np.reshape(car2_state, (1, -1))

            car1_action = self.predict_q_values([master_input, car1_state], CAR1_NAME)
            car2_action = self.predict_q_values([master_input, car2_state], CAR2_NAME)

            if LOG_ACTIONS_SELECTED:
                self.logger.log_actions_selected(self.network, car1_state, car2_state, car1_action, car2_action)

            return car1_action, car2_action

    def set_controls_according_to_sampled_action(self, car_name, sampled_action):
        current_controls = self.airsim.get_car_controls(car_name)
        updated_controls = self.action_to_controls(current_controls, sampled_action)
        self.airsim.set_car_controls(updated_controls, car_name)

    @staticmethod
    def calculate_reward(car1_state, collision_occurred, reached_target, car1_action, car2_action):

        x_car1 = car1_state[0]  # TODO: make it more generic
        cars_distance = car1_state[-1]  # TODO: make it more generic

        # avoid starvation
        reward = STARVATION_REWARD

        # too close
        # x_car1 < 2 is for not punishing after passing without collision (TODO: make it more generic)
        if x_car1 < 2 and cars_distance < SAFETY_DISTANCE_FOR_PUNISH:
            # print(f"too close: {car1_state[-1]}")
            reward = NOT_KEEPING_SAFETY_DISTANCE_REWARD

        # keeping safety distance
        if cars_distance > SAFETY_DISTANCE_FOR_BONUS:
            # print(f"keeping safety distance: {car1_state[-1]}")
            reward = KEEPING_SAFETY_DISTANCE_REWARD

        # reached target
        if reached_target:
            reward = REACHED_TARGET_REWARD

        # collision occurred
        if collision_occurred:
            reward = COLLISION_REWARD

        return reward

    @staticmethod
    def action_to_controls(current_controls, action):
        # translate index of action to controls in car:
        if action == 0:
            current_controls.throttle = 0.75
        elif action == 1:
            current_controls.throttle = 0.4
        return current_controls  # called current_controls - but it is updated controls

    def predict_q_values(self, car_input, car_name):
        q_values = self.network.predict(car_input, verbose=0)
        if LOG_Q_VALUES:
            self.logger.log_q_values(q_values, car_name)
        action_selected = q_values.argmax()
        return action_selected

    def copy_network(self):
        network_copy = create_network_copy(self.network)
        self.network_car2 = network_copy

        if LOG_WEIGHTS_ARE_IDENTICAL:
            are_weights_identical(self.network, self.network_car2)



