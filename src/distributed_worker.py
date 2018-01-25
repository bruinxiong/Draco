from __future__ import print_function
from mpi4py import MPI
import numpy as np

from nn_ops import NN_Trainer

from model_ops.lenet import LeNet, LeNetSplit
from model_ops.resnet import *
from model_ops.resnet_split import *
from model_ops.vgg import *
from model_ops.fc_nn import FC_NN, FC_NN_Split
from model_ops.utils import err_simulation
from compress_gradient import compress
from datasets.utils import get_batch

import torch
from torch.autograd import Variable

import time
from datetime import datetime
import copy
from sys import getsizeof

STEP_START_ = 1
_FACTOR = 23
TAG_LIST_ = [i*30 for i in range(50000)]

def prepare_grad_list(params):
    grad_list = []
    for param_idx, param in enumerate(params):
        # get gradient from layers here
        # in this version we fetch weights at once
        # remember to change type here, which is essential
        #grads = param.grad.data.numpy().astype(np.float64)
        grads = param.grad.data.numpy().astype(np.float64)
        grad_list.append((param_idx, grads))
    return grad_list

def accuracy(output, target, topk=(1,)):
    """Computes the precision@k for the specified values of k"""
    maxk = max(topk)
    batch_size = target.size(0)

    _, pred = output.topk(maxk, 1, True, True)
    pred = pred.t()
    correct = pred.eq(target.view(1, -1).expand_as(pred))

    res = []
    for k in topk:
        correct_k = correct[:k].view(-1).float().sum(0, keepdim=True)
        res.append(correct_k.mul_(100.0 / batch_size))
    return res

class ModelBuffer(object):
    def __init__(self, network):
        """
        this class is used to save model weights received from parameter server
        current step for each layer of model will also be updated here to make sure
        the model is always up-to-date
        """
        self.recv_buf = []
        self.layer_cur_step = []
        '''initialize space to receive model from parameter server'''

        # consider we don't want to update the param of `BatchNorm` layer right now
        # we temporirially deprecate the foregoing version and only update the model
        # parameters
        for param_idx, param in enumerate(network.parameters()):
            self.recv_buf.append(np.zeros(param.size()))
            self.layer_cur_step.append(0)


class DistributedWorker(NN_Trainer):
    def __init__(self, comm, **kwargs):
        self.comm = comm   # get MPI communicator object
        self.world_size = comm.Get_size() # total number of processes
        self.rank = comm.Get_rank() # rank of this Worker
        #self.status = MPI.Status()
        self.cur_step = 0
        self.next_step = 0 # we will fetch this one from parameter server

        self.batch_size = kwargs['batch_size']
        self.max_epochs = kwargs['max_epochs']
        self.momentum = kwargs['momentum']
        self.lr = kwargs['learning_rate']
        self.network_config = kwargs['network']
        self.comm_type = kwargs['comm_method']
        self.kill_threshold = kwargs['kill_threshold']
        self._adversery = kwargs['adversery']
        self._err_mode = kwargs['err_mode']
        self._compress_grad = kwargs['compress_grad']
        self._eval_freq = kwargs['eval_freq']
        self._train_dir = kwargs['train_dir']
        self._checkpoint_step = kwargs['checkpoint_step']        
        self._fail_workers = [self.world_size-i for i in range(1, kwargs['worker_fail']+1)]

        # this one is going to be used to avoid fetch the weights for multiple times
        self._layer_cur_step = []

    def build_model(self):
        # build network
        if self.network_config == "LeNet":
            #self.network=LeNetSplit()
            self.network=LeNet()
        elif self.network_config == "ResNet18":
            self.network=ResNetSplit18()
        elif self.network_config == "ResNet34":
            self.network=ResNetSplit34()
        elif self.network_config == "ResNet50":
            self.network=ResNetSplit50()
        elif self.network_config == "ResNet101":
            self.network=ResNetSplit101()
        elif self.network_config == "ResNet152":
            self.network=ResNetSplit152()
        elif self.network_config == "FC":
            self.network=FC_NN_Split()
        elif self.network_config == "VGG11":
            self.network=vgg11_bn()
        elif self.network_config == "VGG13":
            self.network=vgg13_bn()
        elif self.network_config == "VGG16":
            self.network=vgg16_bn()

        if self._checkpoint_step != 0:
            file_path = "../checkpoints/geo_median/model_step_"+str(self._checkpoint_step)
            self._load_model(file_path)

        # set up optimizer
        self.optimizer = torch.optim.SGD(self.network.parameters(), lr=self.lr, momentum=self.momentum)
        self.criterion = nn.CrossEntropyLoss()
        # assign a buffer for receiving models from parameter server
        self.init_recv_buf()
        #self._param_idx = len(self.network.full_modules)*2-1
        if "ResNet" in self.network_config:
            self._param_idx = self.network.fetch_init_channel_index-1

    def train(self, train_loader, test_loader):
        # the first step we need to do here is to sync fetch the inital worl_step from the parameter server
        # we still need to make sure the value we fetched from parameter server is 1
        global STEP_START_

        self.sync_fetch_step()
        # do some sync check here
        assert(self.update_step())
        if self._checkpoint_step == 0:
            assert(self.cur_step == STEP_START_)
        else:
            assert(self.cur_step == int(self._checkpoint_step)+1)

        # number of batches in one epoch
        num_batch_per_epoch = len(train_loader.dataset) / self.batch_size
        batch_idx = -1
        epoch_idx = 0
        epoch_avg_loss = 0
        iteration_last_step=0
        iter_start_time=0
        first = True

        print("Worker {}: starting training".format(self.rank))
        # start the training process
        for num_epoch in range(self.max_epochs):
            for batch_idx, (train_image_batch, train_label_batch) in enumerate(train_loader):
                X_batch, y_batch = Variable(train_image_batch), Variable(train_label_batch)
                while True:
                    # the worker shouldn't know the current global step
                    # except received the message from parameter server
                    self.async_fetch_step()

                    # the only way every worker know which step they're currently on is to check the cur step variable
                    updated = self.update_step()

                    if (not updated) and (not first):
                        # wait here unitl enter next step
                        continue

                    # the real start point of this iteration
                    iteration_last_step = time.time() - iter_start_time
                    iter_start_time = time.time()
                    first = False
                    print("Rank of this node: {}, Current step: {}".format(self.rank, self.cur_step))

                    # TODO(hwang): return layer request here and do weight before the forward step begins, rather than implement
                    # the wait() in the fetch function
                    fetch_weight_start_time = time.time()
                    if self.comm_type == "Bcast":
                        self.async_fetch_weights_bcast()
                    elif self.comm_type == "Async":
                        self.async_fetch_weights_async()

                    fetch_weight_duration = time.time() - fetch_weight_start_time

                    # switch to training mode
                    self.network.train()
                    # manage batch index manually
                    self.optimizer.zero_grad()
                    # forward step
                    forward_start_time = time.time()
                    logits = self.network(X_batch)
                    if "ResNet" in self.network_config:
                        logits_1 = Variable(logits.data, requires_grad=True)
                        loss = self.criterion(logits_1, y_batch)
                    else:
                        loss = self.criterion(logits, y_batch)
                    epoch_avg_loss += loss.data[0]
                    forward_duration = time.time()-forward_start_time
                    # TODO(hwang): figure out a better way to do this
                    computation_time = time.time() - forward_start_time
                    # backward step
                    backward_start_time = time.time()

                    if "ResNet" in self.network_config:
                        self._backward(loss, logits_1)
                    else:
                        self._backward(loss)
                    '''
                    loss.backward()
                    computation_time = time.time() - forward_start_time
                    # we can send the grad of this very first layer to parameter server right here before
                    # the chain rule is begining
                    req_send_check = []
                    init_grad_data = logits_1.grad.data.numpy()
                    init_grad_data = np.sum(init_grad_data, axis=0).astype(np.float64)
                    # send grad to parameter server
                    if self.rank in self._fail_workers:
                        # simulate some byzantine error here:
                        simulation_grad = err_simulation(grad=init_grad_data, mode=self._err_mode)
                        if self._compress_grad=='compress':
                            _compressed_grad = compress(simulation_grad)
                            req_isend = self.comm.isend(_compressed_grad, dest=0, tag=88+self._param_idx)
                        else:
                            req_isend = self.comm.Isend([simulation_grad, MPI.DOUBLE], dest=0, tag=88+self._param_idx)
                    else:
                        if self._compress_grad=='compress':
                            _compressed_grad = compress(init_grad_data)
                            req_isend = self.comm.isend(_compressed_grad, dest=0, tag=88+self._param_idx)
                        else:
                            req_isend = self.comm.Isend([init_grad_data, MPI.DOUBLE], dest=0, tag=88+self._param_idx)
                    req_send_check.append(req_isend)
                    req_send_check=self.network.backward_normal(logits_1.grad, self.comm, req_send_check, self.cur_step, self._fail_workers, self._err_mode, self._compress_grad)
                    req_send_check[-1].wait()
                    '''
                    backward_duration = time.time()-backward_start_time
                    # on the end of a certain iteration
                    prec1, prec5 = accuracy(logits.data, train_label_batch.long(), topk=(1, 5))
                    print('Worker: {}, Cur Step: {}, Train Epoch: {} [{}/{} ({:.0f}%)], Train Loss: {:.4f}, Time Cost: {:.4f}, Computation Time: {:.4f}, Prec@1: {}, Prec@5: {}'.format(self.rank,
                         self.cur_step, num_epoch, batch_idx * self.batch_size, len(train_loader.dataset), 
                            (100. * (batch_idx * self.batch_size) / len(train_loader.dataset)), loss.data[0], time.time()-iter_start_time, computation_time, prec1.numpy()[0], prec5.numpy()[0]))
                    # break here to fetch data then enter fetching step loop again
                    if self.cur_step%self._eval_freq == 0 and self.rank==1:
                        if "ResNet" in self.network_config:
                            self._evaluate_model(test_loader)
                            self._save_model(file_path=self._generate_model_path())
                        else:
                            pass
                    break

    def init_recv_buf(self):
        self.model_recv_buf = ModelBuffer(self.network)

    def sync_fetch_step(self):
        '''fetch the first step from the parameter server'''
        self.next_step = self.comm.recv(source=0, tag=10)

    def async_fetch_step(self):
        req = self.comm.irecv(source=0, tag=10)
        self.next_step = req.wait()

    def async_fetch_weights_async(self):
        request_layers = []
        layers_to_update = []
        for layer_idx, layer in enumerate(self.model_recv_buf.recv_buf):
            if self.model_recv_buf.layer_cur_step[layer_idx] < self.cur_step:
                layers_to_update.append(layer_idx)
                req = self.comm.Irecv([self.model_recv_buf.recv_buf[layer_idx], MPI.DOUBLE], source=0, tag=11+layer_idx)
                request_layers.append(req)

        assert (len(layers_to_update) == len(request_layers))
        weights_to_update = []
        for req_idx, req_l in enumerate(request_layers):
            req_l.wait()
            weights = self.model_recv_buf.recv_buf[req_idx]
            weights_to_update.append(weights)
            # we also need to update the layer cur step here:
            self.model_recv_buf.layer_cur_step[req_idx] = self.cur_step
        self.model_update(weights_to_update)
    
    def async_fetch_weights_bcast(self):
        layers_to_update = []
        for layer_idx, layer in enumerate(self.model_recv_buf.recv_buf):
            if self.model_recv_buf.layer_cur_step[layer_idx] < self.cur_step:
                layers_to_update.append(layer_idx)
                self.comm.Bcast([self.model_recv_buf.recv_buf[layer_idx], MPI.DOUBLE], root=0)
        weights_to_update = []
        for req_idx, layer_idx in enumerate(layers_to_update):
            weights = self.model_recv_buf.recv_buf[req_idx]
            weights_to_update.append(weights)
            # we also need to update the layer cur step here:
            self.model_recv_buf.layer_cur_step[req_idx] = self.cur_step
        self.model_update(weights_to_update)
    
    def update_step(self):
        '''update local (global) step on worker'''
        changed = (self.cur_step != self.next_step)
        self.cur_step = self.next_step
        return changed

    def model_update(self, weights_to_update):
        """write model fetched from parameter server to local model"""
        new_state_dict = {}
        model_counter_ = 0
        for param_idx,(key_name, param) in enumerate(self.network.state_dict().items()):
            # handle the case that `running_mean` and `running_var` contained in `BatchNorm` layer
            if "running_mean" in key_name or "running_var" in key_name:
                tmp_dict={key_name: param}
            else:
                assert param.size() == weights_to_update[model_counter_].shape
                tmp_dict = {key_name: torch.from_numpy(weights_to_update[model_counter_])}
                model_counter_ += 1
            new_state_dict.update(tmp_dict)
        self.network.load_state_dict(new_state_dict)

    def _backward(self, loss, logits_1=None):
        loss.backward()
        if "ResNet" in self.network_config:
            req_send_check = []
            init_grad_data = logits_1.grad.data.numpy()
            init_grad_data = np.sum(init_grad_data, axis=0).astype(np.float64)
            # send grad to parameter server
            if self.rank in self._fail_workers:
                # simulate some byzantine error here:
                simulation_grad = err_simulation(grad=init_grad_data, mode=self._err_mode)
                if self._compress_grad=='compress':
                    _compressed_grad = compress(simulation_grad)
                    req_isend = self.comm.isend(_compressed_grad, dest=0, tag=88+self._param_idx)
                else:
                    req_isend = self.comm.Isend([simulation_grad, MPI.DOUBLE], dest=0, tag=88+self._param_idx)
            else:
                if self._compress_grad=='compress':
                    _compressed_grad = compress(init_grad_data)
                    req_isend = self.comm.isend(_compressed_grad, dest=0, tag=88+self._param_idx)
                else:
                    req_isend = self.comm.Isend([init_grad_data, MPI.DOUBLE], dest=0, tag=88+self._param_idx)
            req_send_check.append(req_isend)
            req_send_check=self.network.backward_normal(logits_1.grad, self.comm, req_send_check, self.cur_step, self._fail_workers, self._err_mode, self._compress_grad)
            req_send_check[-1].wait()
        else:
            self._send_grads()

    def _send_grads(self):
        req_send_check = []
        for param_index, param in enumerate(self.network.parameters()):
            grad = param.grad.data.numpy().astype(np.float64)
            if len(req_send_check) != 0:
                req_send_check[-1].wait()
            if self.rank in self._fail_workers:
                simulation_grad = err_simulation(grad, self._err_mode)
                _compressed_grad = compress(simulation_grad)
                req_isend = self.comm.isend(_compressed_grad, dest=0, tag=88+param_index)
                req_send_check.append(req_isend)
            else:
                _compressed_grad = compress(grad)
                req_isend = self.comm.isend(_compressed_grad, dest=0, tag=88+param_index)
                req_send_check.append(req_isend)
        req_send_check[-1].wait()

    def _evaluate_model(self, test_loader):
        self.network.eval()
        test_loss = 0
        correct = 0
        prec1_counter_ = prec5_counter_ = batch_counter_ = 0
        for data, y_batch in test_loader:
            data, target = Variable(data, volatile=True), Variable(y_batch)
            output = self.network(data)
            test_loss += F.nll_loss(output, target, size_average=False).data[0] # sum up batch loss
            #pred = output.data.max(1, keepdim=True)[1] # get the index of the max log-probability
            #correct += pred.eq(target.data.view_as(pred)).cpu().sum()
            prec1_tmp, prec5_tmp = accuracy(output.data, y_batch, topk=(1, 5))
            prec1_counter_ += prec1_tmp.numpy()[0]
            prec5_counter_ += prec5_tmp.numpy()[0]
            batch_counter_ += 1
        prec1 = prec1_counter_ / batch_counter_
        prec5 = prec5_counter_ / batch_counter_
        test_loss /= len(test_loader.dataset)
        print('Test set: Average loss: {:.4f}, Prec@1: {} Prec@5: {}'.format(test_loss, prec1, prec5))

    def _generate_model_path(self):
        return self._train_dir+"model_step_"+str(self.cur_step)

    def _save_model(self, file_path):
        with open(file_path, "wb") as f_:
            #torch.save(self.network, f_)
            torch.save(self.network.state_dict(), f_)
        return

    def _load_model(self, file_path):
        model_state_dict=torch.load(file_path)
        self.network.load_state_dict(model_state_dict)
        print("Validation Worker Done Loading Checkpoint from {}".format(file_path))


class CodedWorker(DistributedWorker):
    def __init__(self, comm, **kwargs):
        self.comm = comm   # get MPI communicator object
        self.world_size = comm.Get_size() # total number of processes
        self.rank = comm.Get_rank() # rank of this Worker
        #self.status = MPI.Status()
        self.cur_step = 0
        self.next_step = 0 # we will fetch this one from parameter server

        self.batch_size = kwargs['batch_size']
        self.max_epochs = kwargs['max_epochs']
        self.momentum = kwargs['momentum']
        self.lr = kwargs['learning_rate']
        self.network_config = kwargs['network']
        self.comm_type = kwargs['comm_method']
        self.kill_threshold = kwargs['kill_threshold']
        self._adversery = kwargs['adversery']
        self._err_mode = kwargs['err_mode']
        self._group_list = kwargs['group_list']
        self._err_case = kwargs['err_case']
        self._train_dir = kwargs['train_dir']
        self._eval_freq = kwargs['eval_freq']

        if kwargs['worker_fail'] % len(self._group_list) == 0:
            _fail_per_group = kwargs['worker_fail'] / len(self._group_list)
            self._fail_workers = [g[len(g)-i] for _,g in self._group_list.iteritems() for i in range(1,_fail_per_group+1)]
        elif kwargs['worker_fail'] <= len(self._group_list):
            _fail_per_group = 1
            self._fail_workers = [g[len(g)-i] for _,g in self._group_list.iteritems() for i in range(1,_fail_per_group+1) if i < kwargs['worker_fail']]
        
        self._group_seeds = kwargs['group_seeds'] 
        self._group_num = kwargs['group_num'] # which group this worker belongs to
        self._group_size = len(self._group_list[0])
        self._compress_grad = kwargs['compress_grad']
        # this one is going to be used to avoid fetch the weights for multiple times
        self._layer_cur_step = []

    def build_model(self):
        # build network
        if self.network_config == "LeNet":
            self.network=LeNetSplit()
        elif self.network_config == "ResNet18":
            self.network=ResNetSplit18()
        elif self.network_config == "ResNet34":
            self.network=ResNetSplit34()
        elif self.network_config == "ResNet50":
            self.network=ResNetSplit50()
        elif self.network_config == "FC":
            self.network=FC_NN_Split()

        # set up optimizer
        self.optimizer = torch.optim.SGD(self.network.parameters(), lr=self.lr, momentum=self.momentum)
        self.criterion = nn.CrossEntropyLoss()
        # assign a buffer for receiving models from parameter server
        self.init_recv_buf()
        #self._param_idx = len(self.network.full_modules)*2-1
        self._param_idx = self.network.fetch_init_channel_index-1

    def train(self, train_loader, test_loader):
        # the first step we need to do here is to sync fetch the inital worl_step from the parameter server
        # we still need to make sure the value we fetched from parameter server is 1

        self.sync_fetch_step()
        # do some sync check here
        assert(self.update_step())
        assert(self.cur_step == STEP_START_)

        # number of batches in one epoch
        num_batch_per_epoch = len(train_loader.dataset) / self.batch_size
        batch_idx = -1
        epoch_idx = 0
        epoch_avg_loss = 0
        iteration_last_step = 0
        iter_start_time = 0
        first = True
        iter_avg_prec1 = 0
        iter_avg_prec5 = 0
        # use following flags to achieve letting each worker compute more batches
        should_enter_next = False

        print("Worker {}: starting training".format(self.rank))
        # start the training process
        for num_epoch in range(self.max_epochs):
            # after each epoch we need to make sure workers in the same group re-shuffling using the same seed
            torch.manual_seed(self._group_seeds[self._group_num]+num_epoch)
            for batch_idx, (train_image_batch, train_label_batch) in enumerate(train_loader):
                X_batch, y_batch = Variable(train_image_batch), Variable(train_label_batch)
                while True:
                    # the worker shouldn't know the current global step except received the message from parameter server
                    self.async_fetch_step()
                    # the only way every worker know which step they're currently on is to check the cur step variable
                    updated = self.update_step()
                    if (not updated) and (not first):
                        # wait here unitl enter next step
                        continue
                    # the real start point of this iteration
                    iter_start_time = time.time()
                    first = False
                    should_enter_next = False
                    print("Rank of this node: {}, Current step: {}".format(self.rank, self.cur_step))
                    # TODO(hwang): return layer request here and do weight before the forward step begins, rather 
                    # than implement the wait() in the fetch function
                    fetch_weight_start_time = time.time()
                    if self.comm_type == "Bcast":
                        self.async_fetch_weights_bcast()
                    elif self.comm_type == "Async":
                        self.async_fetch_weights_async()
                    fetch_weight_duration = time.time() - fetch_weight_start_time

                    self.network.train()
                    self.optimizer.zero_grad()
                    # forward step
                    forward_start_time = time.time()
                    logits = self.network(X_batch)

                    logits_1 = Variable(logits.data, requires_grad=True)
                    loss = self.criterion(logits_1, y_batch)
                    forward_duration = time.time()-forward_start_time

                    # backward step
                    backward_start_time = time.time()
                    loss.backward()
                    computation_time = time.time() - forward_start_time

                    init_grad_data = logits_1.grad.data.numpy()
                    init_grad_data = np.sum(init_grad_data, axis=0).astype(np.float64)

                    grads=self.network.backward_coded(logits_1.grad, self.cur_step)

                    if "ResNet" in self.network_config:
                        grads.insert(0,init_grad_data)

                    prec1, prec5 = accuracy(logits.data, train_label_batch.long(), topk=(1, 5))
                    # in current setting each group cotains k workers, we let each worker calculate k same batches
                    self._send_grads(grads)
                    print('Worker: {}, Cur Step: {}, Train Epoch: {} [{}/{} ({:.0f}%)], Train Loss: {:.4f}, Time Cost: {:.4f}, Computation Time: {:.4f}, Prec@1: {}, Prec@5: {}'.format(self.rank,
                         self.cur_step, num_epoch, batch_idx * self.batch_size, len(train_loader.dataset), 
                            (100. * (batch_idx * self.batch_size) / len(train_loader.dataset)), loss.data[0], time.time()-iter_start_time, computation_time, prec1.numpy()[0], prec5.numpy()[0]))
                    if self.cur_step%self._eval_freq == 0 and self.rank==1:
                        #self._save_model(file_path=self._generate_model_path())
                        if "ResNet" in self.network_config:
                            self._evaluate_model(test_loader)
                            self._save_model(file_path=self._generate_model_path())
                        else:
                            pass
                    break

    def _send_grads(self, grads):
        req_send_check = []
        for i, grad in enumerate(reversed(grads)):
            if len(req_send_check) != 0:
                req_send_check[-1].wait()
            if self.rank in self._fail_workers:
                simulation_grad = err_simulation(grad, self._err_mode)
                if self._compress_grad=='compress':
                    _compressed_grad = compress(simulation_grad)
                    req_isend = self.comm.isend(_compressed_grad, dest=0, tag=88+i)
                else:
                    req_isend = self.comm.Isend([simulation_grad, MPI.DOUBLE], dest=0, tag=88+i)
                req_send_check.append(req_isend)
            else:
                if self._compress_grad=='compress':
                    _compressed_grad = compress(grad)
                    req_isend = self.comm.isend(_compressed_grad, dest=0, tag=88+i)
                else:
                    req_isend = self.comm.Isend([grad, MPI.DOUBLE], dest=0, tag=88+i)
                req_send_check.append(req_isend)
        req_send_check[-1].wait()



class CyclicWorker(DistributedWorker):
    def __init__(self, comm, **kwargs):
        self.comm = comm   # get MPI communicator object
        self.world_size = comm.Get_size() # total number of processes
        self.num_workers = self.world_size-1
        self.rank = comm.Get_rank() # rank of this Worker
        #self.status = MPI.Status()
        self.cur_step = 0
        self.next_step = 0 # we will fetch this one from parameter server

        self.batch_size = kwargs['batch_size']
        self.max_epochs = kwargs['max_epochs']
        self.momentum = kwargs['momentum']
        self.lr = kwargs['learning_rate']
        self.network_config = kwargs['network']
        self.comm_type = kwargs['comm_method']
        self._train_dir = kwargs['train_dir']
        self._compress_grad = kwargs['compress_grad']
        self._W = kwargs['encoding_matrix']
        self._fake_W = kwargs['fake_W']
        self._seed = kwargs['seed']
        self._num_fail = kwargs['worker_fail']
        self._eval_freq = kwargs['eval_freq']
        self._hat_s = int(2*self._num_fail+1)
        self._err_mode = kwargs['err_mode']
        # this one is going to be used to avoid fetch the weights for multiple times
        # randomly generate fail worker index
        #self._fail_workers = np.random.choice(np.arange(1, self.num_workers+1), size=self._num_fail, replace=False)
        self._fail_workers = np.arange(1, self._num_fail+1)
        #self._fail_workers = []
        self._layer_cur_step = []
        self._checkpoint_step = 0

    def build_model(self):
        # build network
        if self.network_config == "LeNet":
            self.network=LeNetSplit()
        elif self.network_config == "ResNet18":
            self.network=ResNetSplit18()
        elif self.network_config == "ResNet34":
            self.network=ResNetSplit34()
        elif self.network_config == "ResNet50":
            self.network=ResNetSplit50()
        elif self.network_config == "FC":
            self.network=FC_NN_Split()

        # set up optimizer
        self.optimizer = torch.optim.SGD(self.network.parameters(), lr=self.lr, momentum=self.momentum)
        self.criterion = nn.CrossEntropyLoss()
        # assign a buffer for receiving models from parameter server
        self.init_recv_buf()
        #self._param_idx = len(self.network.full_modules)*2-1
        self._param_idx = self.network.fetch_init_channel_index-1

    def train(self, training_set, test_loader):
        # the first step we need to do here is to sync fetch the inital worl_step from the parameter server
        # we still need to make sure the value we fetched from parameter server is 1
        self.sync_fetch_step()
        # do some sync check here
        assert(self.update_step())
        assert(self.cur_step == STEP_START_)
        # for debug print
        np.set_printoptions(precision=4,linewidth=200.0)

        # number of batches in one epoch
        num_batch_per_epoch = len(training_set) / self.batch_size
        batch_idx = -1
        epoch_idx = 0
        epoch_avg_loss = 0
        iteration_last_step = 0
        iter_start_time = 0
        first = True
        # use following flags to achieve letting each worker compute more batches
        should_enter_next = False

        print("Worker {}: starting training".format(self.rank))
        # start the training process
        for num_epoch in range(self.max_epochs):
            # after each epoch we need to make sure workers in the same group re-shuffling using the same seed
            torch.manual_seed(self._seed+(_FACTOR*num_epoch))
            batch_bias = 0
            batch_idx = 0
            while batch_bias <= len(training_set):
                if batch_bias+self.batch_size*self.num_workers >= len(training_set):
                    break
                gloabl_image_batch, gloabl_label_batch = get_batch(training_set, np.arange(batch_bias, batch_bias+self.batch_size*self.num_workers))
                batch_bias += self.batch_size*self.num_workers
                batch_idx += 1
                grad_collector = {}
                _precision_counter = 0
                # iteration start here:
                while True:
                    # the worker shouldn't know the current global step except received the message from parameter server
                    self.async_fetch_step()
                    # the only way every worker know which step they're currently on is to check the cur step variable
                    updated = self.update_step()
                    if (not updated) and (not first):
                        # wait here unitl enter next step
                        continue
                    # the real start point of this iteration
                    iter_start_time = time.time()
                    first = False
                    should_enter_next = False
                    print("Rank of this node: {}, Current step: {}".format(self.rank, self.cur_step))
                    # TODO(hwang): return layer request here and do weight before the forward step begins, rather 
                    # than implement the wait() in the fetch function
                    fetch_weight_start_time = time.time()

                    # fetch weight
                    self.async_fetch_weights_bcast()
                    fetch_weight_duration = time.time() - fetch_weight_start_time
                    # calculating on coded batches
                    for b in range(self._hat_s):
                        local_batch_indices = np.where(self._fake_W[self.rank-1]!=0)[0]
                        _batch_bias = local_batch_indices[b]*self.batch_size
                        train_image_batch = gloabl_image_batch[_batch_bias:_batch_bias+self.batch_size,:]
                        train_label_batch = gloabl_label_batch[_batch_bias:_batch_bias+self.batch_size]

                        X_batch, y_batch = Variable(train_image_batch), Variable(train_label_batch)
                        self.network.train()
                        self.optimizer.zero_grad()
                        # forward step
                        forward_start_time = time.time()
                        logits = self.network(X_batch)

                        logits_1 = Variable(logits.data, requires_grad=True)
                        loss = self.criterion(logits_1, y_batch)
                        forward_duration = time.time()-forward_start_time

                        # backward step
                        backward_start_time = time.time()
                        loss.backward()
                        computation_time = time.time() - forward_start_time

                        init_grad_data = logits_1.grad.data.numpy()
                        init_grad_data = np.sum(init_grad_data, axis=0).astype(np.float64)
                        grads=self.network.backward_coded(logits_1.grad, self.cur_step)
                        # debug settings for resnet
                        if "ResNet" in self.network_config:
                            grads.insert(0,init_grad_data)
                        # gather each batch calculated by this worker
                        grad_collector[_batch_bias/self.batch_size] = grads
                        _prec1, _ = accuracy(logits.data, train_label_batch.long(), topk=(1, 5))
                        _precision_counter += _prec1.numpy()[0]
                    # send linear combinations of gradients of multiple batches
                    self._send_grads(grad_collector)
                    print('Worker: {}, Cur Step: {}, Train Epoch: {} [{}/{} ({:.0f}%)], Train Loss: {:.4f}, Time Cost: {:.4f}, Computation Time: {:.4f}, Prec@1: {}'.format(self.rank,
                        self.cur_step, num_epoch, batch_idx * self.batch_size, len(training_set), 
                        (100. * (batch_idx * self.batch_size) / len(training_set)), loss.data[0], time.time()-iter_start_time, computation_time, _precision_counter/self._hat_s))
                    if self.cur_step%self._eval_freq == 0 and self.rank==1:
                        if "ResNet" in self.network_config:
                            self._evaluate_model(test_loader)
                            self._save_model(file_path=self._generate_model_path())
                        else:
                            pass
                    break

    def _send_grads(self, grad_collector):
        '''
        note that at here we're not sending anything about gradient but linear combination of gradients
        '''
        req_send_check = []
        for i, param in enumerate(reversed(grad_collector[grad_collector.keys()[0]])):
            aggregated_grad = np.zeros(param.shape, dtype=complex)
            # calculate combined gradients
            for k, v in grad_collector.iteritems():
                aggregated_grad = np.add(aggregated_grad, np.dot(self._W[self.rank-1][k], v[len(v)-i-1]))
            # send grad to master
            if len(req_send_check) != 0:
                req_send_check[-1].wait()
            if self.rank in self._fail_workers:
                simulation_grad = err_simulation(aggregated_grad, self._err_mode, cyclic=True)
                _compressed_grad = compress(simulation_grad)
                req_isend = self.comm.isend(_compressed_grad, dest=0, tag=88+i)
                req_send_check.append(req_isend)
            else:
                _compressed_grad = compress(aggregated_grad)
                req_isend = self.comm.isend(_compressed_grad, dest=0, tag=88+i)
                req_send_check.append(req_isend)
        req_send_check[-1].wait()

if __name__ == "__main__":
    # this is only a simple test case
    comm = MPI.COMM_WORLD
    rank = comm.Get_rank()
    world_size = comm.Get_size()
    worker_fc_nn = WorkerFC_NN(comm=comm, world_size=world_size, rank=rank)
    print("I am worker: {} in all {} workers".format(worker_fc_nn.rank, worker_fc_nn.world_size))