import os
import sys
import numpy as np
import tensorflow as tf

sys.path.insert(1, os.path.join(sys.path[0],'..'))

from util.vgg import vgg_net
from util import helper
from factorcell import FactorCell
from vocab import Vocab


class MetaQACModel(object):
    """Helper class for loading models."""

    def __init__(self, expdir):
        self.expdir = expdir
        self.params = helper.GetParams(os.path.join(expdir, 'char_vocab.pickle'), 'eval',
                                       expdir)
        # mapping of characters to indices
        self.char_vocab = Vocab.Load(os.path.join(expdir, 'char_vocab.pickle'))
        self.params.vocab_size = len(self.char_vocab)
        # construct the tensorflow graph
        self.graph = tf.Graph()
        with self.graph.as_default():
            self.model = QACModel(self.params, training_mode=False)
            self.char_tensor = tf.constant(self.char_vocab.GetWords(), name='char_tensor')
            self.beam_chars = tf.nn.embedding_lookup(self.char_tensor, self.model.selected)


    def Lock(self, image):

        self.session.run(self.model.decoder_cell.lock_op,
                         {self.model.images: image[np.newaxis, :]})


    def MakeSession(self, threads=8):
        """Create the session with the given number of threads."""
        config = tf.ConfigProto(inter_op_parallelism_threads=threads,
                                intra_op_parallelism_threads=threads)
        with self.graph.as_default():
            self.session = tf.Session(config=config)

    def Restore(self):
        """Initialize all variables and restore model from disk."""
        with self.graph.as_default():
            saver = tf.train.Saver(tf.trainable_variables())
            self.session.run(tf.global_variables_initializer())
            saver.restore(self.session, os.path.join(self.expdir, 'model.bin'))

    def MakeSessionAndRestore(self, threads=8):
        self.MakeSession(threads)
        self.Restore()


class QACModel(object):
    """Defines the Tensorflow graph for training and testing a model."""

    def __init__(self, params, training_mode=True, optimizer=tf.train.AdamOptimizer,only_char = False):
        self.params = params
        self.only_char = only_char
        opt = optimizer(params.learning_rate)
        self.BuildGraph(params, training_mode=training_mode, optimizer=opt)
        if not training_mode:
            self.BuildDecoderGraph()

    def BuildGraph(self, params, training_mode=True, optimizer=None):

        self.queries = tf.placeholder(tf.int32, [None, params.max_len], name='queries')
        self.query_lengths = tf.placeholder(tf.int32, [None], name='query_lengths')
        self.dropout_keep_prob = tf.placeholder_with_default(1.0, (), name='keep_prob')

        x = self.queries[:, :-1]  # strip off the end of query token
        y = self.queries[:, 1:]  # need to predict y from x

        with tf.variable_scope('QAC'):

            with tf.variable_scope('char'):
                self.char_embeddings = tf.get_variable(
                    'char_embeddings', [params.vocab_size, params.char_embed_size])
                self.char_bias = tf.get_variable('char_bias', [params.vocab_size])

            inputs = tf.nn.embedding_lookup(self.char_embeddings, x)

            if self.only_char:
                self.images = tf.placeholder(tf.float32, [None, params.image_feat_size])
                self.vgg_placeholder = tf.get_variable("vgg_placeholder", [params.image_feat_size,params.image_feat_size], initializer=tf.zeros_initializer())
                self.vgg_feat = tf.matmul(self.images, self.vgg_placeholder)
                print(self.vgg_feat.get_shape().as_list())
            else:

                self.images = tf.placeholder(tf.float32, [None, params.img_size, params.img_size, 3])
                if params.vgg_feat_type =='full':
                    if training_mode:
                        self.vgg_feat = vgg_net.vgg_fc8(self.images, 'vgg_local',
                                                    apply_dropout=params.apply_dropout,
                                                    output_dim=params.image_feat_size,
                                                    initialize = False)
                    else:
                        self.vgg_feat = vgg_net.vgg_fc8(self.images, 'vgg_local',
                                                    apply_dropout=False,
                                                    output_dim=params.image_feat_size,
                                                    initialize = False)


            # create a mask to zero out the loss for the padding tokens
            indicator = tf.sequence_mask(self.query_lengths - 1, params.max_len - 1)
            _mask = tf.where(indicator, tf.ones_like(x, dtype=tf.float32),
                             tf.zeros_like(x, dtype=tf.float32))

            with tf.variable_scope('rnn'):
                self.decoder_cell = FactorCell(params.num_units, params.char_embed_size,
                                               self.vgg_feat,
                                               bias_adaptation=params.use_mikolov_adaptation,
                                               lowrank_adaptation=params.use_lowrank_adaptation,
                                               rank=params.rank,
                                               layer_norm=params.use_layer_norm,
                                               dropout_keep_prob=self.dropout_keep_prob)

                outputs, _ = tf.nn.dynamic_rnn(self.decoder_cell, inputs,
                                               sequence_length=self.query_lengths,
                                               dtype=tf.float32)
                # reshape outputs to 2d before passing to the output layer
                reshaped_outputs = tf.reshape(outputs, [-1, params.num_units])
                projected_outputs = tf.layers.dense(reshaped_outputs, params.char_embed_size,
                                                    name='proj')
                reshaped_logits = tf.matmul(projected_outputs, self.char_embeddings,
                                            transpose_b=True) + self.char_bias

            reshaped_labels = tf.reshape(y, [-1])
            reshaped_mask = tf.reshape(_mask, [-1])

            reshaped_loss = tf.nn.sparse_softmax_cross_entropy_with_logits(
                logits=reshaped_logits, labels=reshaped_labels)
            masked_loss = tf.multiply(reshaped_loss, reshaped_mask)

            # reshape the loss back to the input size in order to compute
            # the per sentence loss
            self.per_word_loss = tf.reshape(masked_loss, tf.shape(x))
            self.per_sentence_loss = tf.div(tf.reduce_sum(self.per_word_loss, 1),
                                            tf.reduce_sum(_mask, 1))
            self.per_sentence_loss = tf.reduce_sum(self.per_word_loss, 1)

            total_loss = tf.reduce_sum(masked_loss)
            self.words_in_batch = tf.to_float(tf.reduce_sum(self.query_lengths - 1))
            self.avg_loss = total_loss / self.words_in_batch


        # don't train conv layers
        if training_mode:
            tvars = tf.trainable_variables()
            if self.only_char:
                non_vgg_conv_vars = [var for var in tvars if 'vgg' not in var.name]
            else:
                # Only retrain last two layers
                non_vgg_conv_vars = filter(lambda x: not x.startswith(('QAC/vgg_local/conv','QAC/vgg_local/fc6')),
                                           [var.name for var in tvars])
                non_vgg_conv_vars = [var for var in tvars if var.name in non_vgg_conv_vars]

            self.train_op = optimizer.minimize(self.avg_loss, var_list=non_vgg_conv_vars)


    def LoadVGG(self, session, pretrained_path = ''):

        vgg_weights = np.load(pretrained_path)

        vgg_W = vgg_weights['processed_W'].item()
        vgg_B = vgg_weights['processed_B'].item()

        do_not_load = ['fc7','fc8']

        for name in vgg_W:
            if name not in do_not_load:
                print(name)
                weight = [w for w in tf.trainable_variables() if name in w.name and 'weight' in w.name][0]
                print(weight)
                session.run(weight.assign(vgg_W[name]))

        for name in vgg_B:
            if name not in do_not_load:
                print(name)
                weight = [w for w in tf.trainable_variables() if name in w.name and 'biases' in w.name][0]
                print(weight)
                session.run(weight.assign(vgg_B[name]))

    def BuildDecoderGraph(self):
        """This part of the graph is only used for evaluation.

        It computes just a single step of the LSTM.
        """
        with tf.variable_scope('QAC'):

            self.prev_word = tf.placeholder(tf.int32, [None], name='prev_word')
            self.prev_hidden_state = tf.placeholder(
                tf.float32, [None, 2 * self.params.num_units], name='prev_hidden_state')
            prev_c = self.prev_hidden_state[:, :self.params.num_units]
            prev_h = self.prev_hidden_state[:, self.params.num_units:]

            # temperature can be used to tune diversity of the decoding
            self.temperature = tf.placeholder_with_default([1.0], [1])

            prev_embed = tf.nn.embedding_lookup(self.char_embeddings, self.prev_word)

            state = tf.nn.rnn_cell.LSTMStateTuple(prev_c, prev_h)
            # result, (next_c, next_h) = self.decoder_cell(
            #     prev_embed, state, use_locked=True)
            result, (next_c, next_h) = self.decoder_cell(
                prev_embed, state, use_locked=True)

            self.next_hidden_state = tf.concat([next_c, next_h], 1)

            with tf.variable_scope('rnn', reuse=True):
                proj_result = tf.layers.dense(
                    result, self.params.char_embed_size, reuse=True, name='proj')
            logits = tf.matmul(proj_result, self.char_embeddings,
                               transpose_b=True) + self.char_bias
            prevent_unk = tf.one_hot([0], self.params.vocab_size, -30.0)
            self.next_prob = tf.nn.softmax(prevent_unk + logits / self.temperature)
            self.next_log_prob = tf.nn.log_softmax(logits / self.temperature)

            # return the top `beam_size` number of characters for use in decoding
            self.beam_size = tf.placeholder_with_default(1, (), name='beam_size')
            log_probs, self.selected = tf.nn.top_k(self.next_log_prob, self.beam_size)
            self.selected_p = -log_probs  # cost is the negative log likelihood
