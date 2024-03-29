from keras import Input, Model
from keras.layers import Dense, concatenate
from experience import experience
from datetime import datetime
import tensorflow as tf
from tensorflow import keras
from utils.environment_utils import *

class RL:

    def __init__(self, learning_rate, verbose, with_per, experiment_id, alternate_training, alternate_car):
        self.step_counter = 0
        self.verbose = verbose
        self.learning_rate = learning_rate
        self.opt = tf.keras.optimizers.legacy.Adam(learning_rate=self.learning_rate)
        self.env_state = None
        self.c1_state = None
        self.c2_state = None
        self.c1_desire = np.array([10, 0])
        self.global_state = None
        self.local_network = self.init_local_network()
        self.local_and_global_network = self.init_local_and_global_network()
        # experience replay
        self.experiences_size = 10000
        self.exp_batch_size = 10
        self.with_per = with_per  # per = prioritized experience replay (if 1 is on, if 0 off and use TD error only)
        self.experiences = experience.Experiences(self.experiences_size, self.with_per)
        self.gamma = 0.95
        self.epsilon = 0.9
        self.epsilon_decay = 0.99
        ##
        self.alternate_training = alternate_training
        if not alternate_training:
            self.alternate_training_network = None
        else:
            self.alternate_training_network = self.init_local_network()
        self.alternate_car = alternate_car  # The car which continues to train
        # define log directory for loss + init tensor board for loss graph
        log_dir_for_tensorboard = "experiments/" + experiment_id + "/loss/" + datetime.now().strftime("%Y%m%d-%H%M%S")
        self.tensorboard = tf.summary.create_file_writer(log_dir_for_tensorboard)
        #
        self.train_global_counter = 0
        self.train_global_loss_sum = 0
        #

    def init_local_and_global_network(self):
        # source of building the network:
        # https://pyimagesearch.com/2019/02/04/keras-multiple-inputs-and-mixed-data/

        # define the input of global network:
        input_global = Input(shape=(7,))  # (x_car1, y_car1, x_car2, y_car2, v_car1, v_car2, dist_c1_c2)
        # define the global network layers:
        x = Dense(16, activation="relu")(input_global)
        x = Dense(8, activation="relu")(x)
        x = Dense(2, activation="relu")(x)
        x = Model(inputs=input_global, outputs=x)  # (emb1, emb2) = output of global

        input_local = Input(
            shape=(9,))  # (x_car, y_car, v_car1, v_car2, up_car2,down_car2,right_car2,left_car2, dist_c1_c2)
        combined = concatenate([x.output, input_local])  # combine embedding of global & input of local

        z = Dense(16, activation="relu")(combined)
        z = Dense(8, activation="relu")(z)
        z = Dense(2, activation="linear")(z)
        model = Model(inputs=[x.input, input_local], outputs=z)  # (q_value1, q_value2) = output of whole network

        model.compile(optimizer=tf.keras.optimizers.legacy.Adam(learning_rate=self.learning_rate), loss="mse")
        return model

    def init_local_network(self):
        """
        input of network: (x_car, y_car, v_car1, v_car2, up_car2,down_car2,right_car2,left_car2, dist_c1_c2)
        output of network: (q_value1, q_value2)
        """
        network = keras.Sequential([
            keras.layers.InputLayer(input_shape=(9,)),
            keras.layers.Normalization(axis=-1),
            keras.layers.Dense(units=16, activation='relu', kernel_initializer=tf.keras.initializers.HeUniform()),
            keras.layers.Dense(units=8, activation='relu', kernel_initializer=tf.keras.initializers.HeUniform()),
            keras.layers.Dense(units=2, activation='linear')
        ])
        network.compile(optimizer=tf.keras.optimizers.legacy.Adam(learning_rate=self.learning_rate), loss="mse")
        return network

    def copy_network(self, network):
        return keras.models.clone_model(network)

    # TODO: create more readable code and go over logic with gilad
    def step_local_2_cars(self, airsim_client, steps_counter):

        self.step_counter += 1

        # get state from environment of car1 and car2
        self.env_state = get_env_state(airsim_client, "Car1")
        # update state of car1 (to be fed as input to the DNN):
        self.c1_state = np.array([[self.env_state["x_c1"],
                                   self.env_state["y_c1"],
                                   self.env_state["v_c1"],
                                   self.env_state["v_c2"],
                                   self.env_state["dist_c1_c2"],
                                   self.env_state["right"],
                                   self.env_state["left"],
                                   self.env_state["forward"],
                                   self.env_state["backward"]
                                   ]])  # has to be [[]] to enter as input to the DNN

        self.env_state = get_env_state(airsim_client, "Car2")
        self.c2_state = np.array([[self.env_state["x_c2"],
                                   self.env_state["y_c2"],
                                   self.env_state["v_c2"],
                                   self.env_state["v_c1"],
                                   self.env_state["dist_c1_c2"],
                                   self.env_state["right"],
                                   self.env_state["left"],
                                   self.env_state["forward"],
                                   self.env_state["backward"]
                                   ]])  # has to be [[]] to enter as input to the DNN

        # Detect Collision:
        collision = False
        collision_info = airsim_client.simGetCollisionInfo()
        if collision_info.has_collided:
            collision = True
            return collision, None, None, None, -1000

        # sample actions from network
        action_car1, action_car2 = self.sample_action_by_epsilon_greedy()

        if self.alternate_training:
            if self.alternate_car == 1:
                target = self.local_network.predict(self.c1_state, verbose=self.verbose)
                self.env_state = get_env_state(airsim_client, "Car1")
                # get new state
                self.c1_state = np.array([[self.env_state["x_c1"],
                                           self.env_state["y_c1"],
                                           self.env_state["v_c1"],
                                           self.env_state["v_c2"],
                                           self.env_state["dist_c1_c2"],
                                           self.env_state["right"],
                                           self.env_state["left"],
                                           self.env_state["forward"],
                                           self.env_state["backward"]
                                           ]])  # has to be [[]] to enter as input to the DNN

                reward, reached_target = self.calc_reward(collision)

                # update q values - train the local network so it will continue to train.
                q_future = np.max(self.local_network.predict(self.c1_state, verbose=self.verbose)[0])
                target[0][action_car1] += self.learning_rate * (reward + (q_future * 0.95) - target[0][action_car1])

                loss_local = self.local_network.fit(self.c1_state, target, epochs=1, verbose=0)
                # print(f'Loss = {loss_local.history["loss"][-1]}')

                if not reached_target:
                    with self.tensorboard.as_default():
                        tf.summary.scalar('loss', loss_local.history["loss"][-1], step=steps_counter)

            if self.alternate_car == 2:
                target = self.local_network.predict(self.c2_state, verbose=self.verbose)
                self.env_state = get_env_state(airsim_client, "Car2")
                self.c2_state = np.array([[self.env_state["x_c2"],
                                           self.env_state["y_c2"],
                                           self.env_state["v_c2"],
                                           self.env_state["v_c1"],
                                           self.env_state["dist_c1_c2"],
                                           self.env_state["right"],
                                           self.env_state["left"],
                                           self.env_state["forward"],
                                           self.env_state["backward"]
                                           ]])  # has to be [[]] to enter as input to the DNN

                reward, reached_target = self.calc_reward(collision)

                # update q values - train the local network, so it will continue to train.
                q_future = np.max(self.local_network.predict(self.c2_state, verbose=self.verbose)[0])
                target[0][action_car2] += self.learning_rate * (reward + (q_future * 0.95) - target[0][action_car2])

                loss_local = self.local_network.fit(self.c2_state, target, epochs=1, verbose=0)
                # print(f'Loss = {loss_local.history["loss"][-1]}')

                if not reached_target:
                    with self.tensorboard.as_default():
                        tf.summary.scalar('loss', loss_local.history["loss"][-1], step=steps_counter)

        if (self.step_counter % 5) == 0:
            self.epsilon *= self.epsilon_decay
            # print(f"Epsilon = {self.epsilon}")

        # Set controls based action selected:
        current_controls_car1 = airsim_client.getCarControls("Car1")
        updated_controls_car1 = self.action_to_controls(current_controls_car1, action_car1)

        current_controls_car2 = airsim_client.getCarControls("Car2")
        updated_controls_car2 = self.action_to_controls(current_controls_car2, action_car2)

        return collision, reached_target, updated_controls_car1, updated_controls_car2, reward

    def step_with_global(self, airsim_client, steps_counter):

        # increase the step counter for tensorboard logging
        self.step_counter += 1

        # get state of car1
        self.env_state = get_env_state(airsim_client, "Car1")

        # update state of car1 (to be fed as input to the DNN):
        self.c1_state = np.array([[self.env_state["x_c1"],
                                   self.env_state["y_c1"],
                                   self.env_state["v_c1"],
                                   self.env_state["v_c2"],
                                   self.env_state["dist_c1_c2"],
                                   self.env_state["right"],
                                   self.env_state["left"],
                                   self.env_state["forward"],
                                   self.env_state["backward"]
                                   ]])  # has to be [[]] to enter as input to the DNN

        # get state of car2
        self.env_state = get_env_state(airsim_client, "Car2")

        # update state of car2 (to be fed as input to the DNN):
        self.c2_state = np.array([[self.env_state["x_c2"],
                                   self.env_state["y_c2"],
                                   self.env_state["v_c2"],
                                   self.env_state["v_c1"],
                                   self.env_state["dist_c1_c2"],
                                   self.env_state["right"],
                                   self.env_state["left"],
                                   self.env_state["forward"],
                                   self.env_state["backward"]
                                   ]])  # has to be [[]] to enter as input to the DNN

        # update the global state:
        self.global_state = np.array([[self.env_state["x_c1"],
                                       self.env_state["y_c1"],
                                       self.env_state["x_c2"],
                                       self.env_state["y_c2"],
                                       self.env_state["v_c1"],
                                       self.env_state["v_c2"],
                                       self.env_state["dist_c1_c2"],
                                       ]])  # has to be [[]] to enter as input to the DNN

        # Detect Collision:
        collision = False
        collision_info = airsim_client.simGetCollisionInfo()
        if collision_info.has_collided:
            print("Collided!")
            collision = True
            reward, reached_target = self.calc_reward(collision)
            return collision, None, None, reward

        # sample action for car1 and car2:
        action_car1, action_car2 = self.sample_action_global()

        # TODO: how to update the network - based on car1 state or both cars?
        target = self.local_and_global_network.predict([self.global_state, self.c1_state], verbose=self.verbose)
        # print(f"Target: {target}")

        self.env_state = get_env_state(airsim_client, "Car1")
        # get new state of car1 after performing the :
        new_state = np.array([[self.env_state["x_c1"],
                               self.env_state["y_c1"],
                               self.env_state["v_c1"],
                               self.env_state["dist_c1_c2"]
                               ]])
        new_global = np.array([[self.env_state["x_c1"],
                                self.env_state["y_c1"],
                                self.env_state["x_c2"],
                                self.env_state["y_c2"]
                                ]])

        reward, reached_target = self.calc_reward(collision)

        # if not using batch:
        state = state[0]
        action = action[0]
        reward = reward[0]
        done = done[0]
        new_state = new_state[0]

        # update q values
        q_future = np.max(self.local_and_global_network.predict([new_global, new_state], verbose=self.verbose)[0])
        target[0][action] += self.learning_rate * (reward + (q_future * 0.95) - target[0][action])

        loss_global = self.local_and_global_network.fit([self.global_state, self.c1_state], target, epochs=1, verbose=0)
        print(f'Loss: {loss_global.history["loss"][-1]}')

        if not reached_target:
            with self.tensorboard.as_default():
                tf.summary.scalar('loss', loss_global.history["loss"][-1], step=steps_counter)

        # Set controls based action selected:
        current_controls = airsim_client.getCarControls()
        updated_controls = self.action_to_controls(current_controls, action)
        return collision, reached_target, updated_controls, reward

    def sample_action_by_epsilon_greedy(self):
        if np.random.binomial(1, p=self.epsilon):
            # pick random action
            rand_action = np.random.randint(2, size=(1, 1))[0][0]
            # print(f"Random Action: {rand_action}")
            return rand_action, rand_action
        else:
            if not self.alternate_training:
                # pick the action based on the highest q value
                action_selected_car1 = self.local_network.predict(self.c1_state, verbose=self.verbose).argmax()
                action_selected_car2 = self.local_network.predict(self.c2_state, verbose=self.verbose).argmax()
                # print(f"Selected Action: {action_selected}")
                return action_selected_car1, action_selected_car2
            else:
                if self.alternate_car == 1:
                    action_selected_car1 = self.local_network.predict(self.c1_state, verbose=self.verbose).argmax()
                    action_selected_car2 = self.alternate_training_network.predict(self.c2_state,
                                                                                   verbose=self.verbose).argmax()
                    # print(f"Selected Action: {action_selected}")
                    return action_selected_car1, action_selected_car2
                if self.alternate_car == 2:
                    action_selected_car1 = self.alternate_training_network.predict(self.c1_state,
                                                                                   verbose=self.verbose).argmax()
                    action_selected_car2 = self.local_network.predict(self.c2_state, verbose=self.verbose).argmax()
                    # print(f"Selected Action: {action_selected}")
                    return action_selected_car1, action_selected_car2

    def sample_action_global(self):
        if np.random.binomial(1, p=self.epsilon):
            # pick random actions for both vechiles
            rand_action = np.random.randint(2, size=(1, 1))[0][0]
            # print(f"Random Action: {rand_action}")
            return rand_action, rand_action
        else:
            # pick the action based on the highest q value
            action_selected_car1 = self.local_and_global_network.predict([self.global_state, self.c1_state],
                                                                         verbose=self.verbose).argmax()
            action_selected_car2 = self.local_and_global_network.predict([self.global_state, self.c2_state],
                                                                         verbose=self.verbose).argmax()
            return action_selected_car1, action_selected_car2

    def action_to_controls(self, current_controls, action):
        # translate index of action to controls in car:
        if action == 0:
            current_controls.throttle = 0.75
        elif action == 1:
            current_controls.throttle = 0.4
        return current_controls  # called current_controls - but it is updated controls

    def calc_reward(self, collision):
        # constant punish for every step taken, big punish for collision, big reward for reaching the target.
        # reward = 0
        reward = -0.1
        if collision:
            reward -= 1000

        # reward += self.env_state["dist_c1_c2"]**2

        if self.env_state["dist_c1_c2"] < 70:  # maybe the more punish- he wants to finish faster
            # print("too close!!")
            reward -= 150

        if self.env_state["dist_c1_c2"] > 100:
            # print("bonus!!")
            reward += 60

        reached_target = False
        # c1_dist_to_destination = np.sum(np.square(np.array([[self.c1_state[0][0], self.c1_state[0][1]]]) - self.c1_desire))
        # if c1_dist_to_destination <= 150:
        #     reward += 500 / c1_dist_to_destination

        if self.c1_state[0][0] > self.c1_desire[0]:  # if went over the desired
            reward += 1000
            reached_target = True
            # print("Reached Target!!!")

        return reward, reached_target


