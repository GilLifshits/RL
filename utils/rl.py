from keras.callbacks import Callback
import tensorflow as tf
from RL.config import LEARNING_RATE, CAR1_NAME, CAR2_NAME, COLLISION_REWARD, REACHED_TARGET_REWARD, STARVATION_REWARD, \
    NOT_KEEPING_SAFETY_DISTANCE_REWARD, KEEPING_SAFETY_DISTANCE_REWARD, SAFETY_DISTANCE_FOR_PUNISH, \
    SAFETY_DISTANCE_FOR_BONUS, BATCH_SIZE_FOR_TRAJECTORY_BATCH, EPSILON_DECAY
from RL.utils.NN_utils import *
from RL.utils.sanity_checks import *


class RL:

    def __init__(self, logger, airsim):
        self.logger = logger
        self.airsim = airsim
        self.optimizer = tf.keras.optimizers.legacy.Adam(learning_rate=LEARNING_RATE)
        self.discount_factor = 0.95
        self.epsilon = 0.9
        self.epsilon_decay = EPSILON_DECAY
        self.network = init_network(self.optimizer)
        self.current_trajectory = []
        self.trajectories = []
        self.batch_size = BATCH_SIZE_FOR_TRAJECTORY_BATCH  # TODO: relevant for train_batch_of_trajectories function
        self.freeze_master = False

    def step(self):
        """
        Main Idea: get current state, sample action, get next state, get reward, detect collision_occurred or reached_target
        returns: current_state, cars_actions, next_state, collision_occurred, reached_target, reward
        """

        # get current state
        car1_state = self.airsim.get_car1_state()
        car2_state = self.airsim.get_car2_state()

        # sample actions
        car1_action, car2_action = self.sample_action(car1_state, car2_state)

        # set updated controls according to sampled action
        self.set_controls_according_to_sampled_action(CAR1_NAME, car1_action)
        self.set_controls_according_to_sampled_action(CAR2_NAME, car2_action)

        # delay code in order to physically get the next state in the simulator
        # TODO: this sleep time effects how many times in an episode the speed changes (maybe log that)
        # time.sleep(0.1)

        # get next state
        car1_next_state = self.airsim.get_car1_state()
        car2_next_state = self.airsim.get_car2_state()

        # calculate reward
        collision_occurred = self.airsim.collision_occurred()
        reached_target = self.airsim.has_reached_target(car1_next_state)
        reward = self.calculate_reward(car1_next_state, collision_occurred, reached_target)

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

        if train_only_last_step:
            current_trajectory = self.current_trajectory[-2:] # train on the last 2 items (these are the items that describe the last step)
        else:
            current_trajectory = self.current_trajectory

        # Convert the trajectory into separate lists for each component
        states, actions, next_states, rewards = zip(*current_trajectory)

        # Assemble current_state_of_all_trajectory + current_state_q_values_of_all_trajectory
        # predict expects: [array(x, 18),array(x, 9)]=[array of arrays of master input, array of arrays of agent input]
        master_inputs_of_all_trajectory = np.array([np.concatenate(state[0]) for state in states])
        agent_inputs_of_all_trajectory = np.array([state[1] for state in states])
        current_state_of_all_trajectory = [master_inputs_of_all_trajectory, agent_inputs_of_all_trajectory]
        current_state_q_values_of_all_trajectory = self.network.predict(current_state_of_all_trajectory, verbose=0)

        # Assemble next_state_of_all_trajectory + next_state_q_values_of_all_trajectory
        # predict expects: [array(x, 18),array(x, 9)]=[array of arrays of master input, array of arrays of agent input]
        master_inputs_of_next_state_of_all_trajectory = np.array([np.concatenate(next_state[0]) for next_state in next_states])
        agent_inputs_of_next_state_of_all_trajectory = np.array([next_state[1] for next_state in next_states])
        next_state_of_all_trajectory = [master_inputs_of_next_state_of_all_trajectory, agent_inputs_of_next_state_of_all_trajectory]
        next_state_q_values_of_all_trajectory = self.network.predict(next_state_of_all_trajectory, verbose=0)

        # Update Q-values using the DQN update rule for each step in the trajectory
        max_next_q_values = np.max(next_state_q_values_of_all_trajectory, axis=1)
        targets = rewards + self.discount_factor * max_next_q_values
        for i, action in enumerate(actions):
            current_state_q_values_of_all_trajectory[i][action] += LEARNING_RATE * (targets[i] - current_state_q_values_of_all_trajectory[i][action])


        # TODO: organize this code - maybe move to another function? create one option with saving gradient and one without?
        # TODO: check that the loss value is the same as commented code below
        # TODO: make sure that the gradients are applied and the weights are changing (in both versions!!)
        with tf.GradientTape() as tape:
            predictions = self.network(current_state_of_all_trajectory, training=True)
            loss = tf.keras.losses.mean_squared_error(current_state_q_values_of_all_trajectory, predictions)
        gradients = tape.gradient(loss, self.network.trainable_variables)

        for grad, var in zip(gradients, self.network.trainable_variables):
            print(f"Gradient for {var.name}: {grad}")

        # Apply gradients
        self.network.optimizer.apply_gradients(zip(gradients, self.network.trainable_variables))

        # TODO: move this to function
        # TODO: organize the directories of logs
        # TODO: to do so, edit the logger.py code, create another directory for weights and gradients
            # remove global_experiment and local_experiment for now
            # rewards_and_losses (with directory for each data_time)
            # weights and gradients (with directory for each data_time)
            # saved_weights
        # TODO: pay attention it is only trainable variables
        # TODO: write documentation in notion about reading these graphs
            # use https://www.youtube.com/watch?v=pSexXMdruFM&ab_channel=deeplizard
            # or use documentation about histograms of tensorboard
        # TODO: create code to control how often we right the log (maybe not in each episode) every 5 episodes for example
        summary_writer = tf.summary.create_file_writer('experiments/')
        with summary_writer.as_default():
            for var in self.network.trainable_variables:
                tf.summary.histogram(var.name, var, step=episode_counter)
            for grad, var in zip(gradients, self.network.trainable_variables):
                tf.summary.histogram(f'{var.name}_gradient', grad, step=episode_counter)

        return loss.numpy().mean()

        # # Batch update the network
        # # fit expects [array(x, 18),array(x, 9)]=[array of arrays of master input, array of arrays of agent input],
        # #             [array(x, 2)] = [array of q-values for each state in trajectory]
        # loss_object = self.network.fit(current_state_of_all_trajectory, current_state_q_values_of_all_trajectory,
        #                                batch_size=len(states), verbose=0, epochs=1)
        # loss_value = loss_object.history["loss"][-1]
        # return loss_value

    def sample_action(self, car1_state, car2_state):

        if np.random.binomial(1, p=self.epsilon):  # epsilon greedy
            # TODO: delete after sanity check
            print("random action")

            car1_random_action = np.random.randint(2, size=(1, 1))[0][0]
            car2_random_action = np.random.randint(2, size=(1, 1))[0][0]
            return car1_random_action, car2_random_action
        else:
            master_input = np.concatenate((car1_state, car2_state), axis=0).reshape(1, -1)
            car1_state = np.reshape(car1_state, (1, -1))
            car2_state = np.reshape(car2_state, (1, -1))

            car1_action = self.network.predict([master_input, car1_state], verbose=0).argmax()
            car2_action = self.network.predict([master_input, car2_state], verbose=0).argmax()

            # TODO: delete after sanity check
            check_effect_of_master_on_selected_actions(self.network, car1_state, car2_state, car1_action, car2_action)

            return car1_action, car2_action

    def set_controls_according_to_sampled_action(self, car_name, sampled_action):
        current_controls = self.airsim.get_car_controls(car_name)
        updated_controls = self.action_to_controls(current_controls, sampled_action)
        self.airsim.set_car_controls(updated_controls, car_name)

    @staticmethod
    def calculate_reward(car1_state, collision_occurred, reached_target):

        # avoid starvation
        reward = STARVATION_REWARD

        # too close
        # TODO: change car1_state[-1] to be more generic
        if car1_state[-1] < SAFETY_DISTANCE_FOR_PUNISH:
            reward = NOT_KEEPING_SAFETY_DISTANCE_REWARD

        # keeping safety distance
        # TODO: change car1_state[-1] to be more generic
        if car1_state[-1] > SAFETY_DISTANCE_FOR_BONUS:
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



