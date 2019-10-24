# 1. data loading: split trainning data to clients
# 2. In each training epoch, do #clients training on each small dataset. then combine and compute the gradients
# 3. what need to be compared?
# 4. considering using gpu for acceleration
import argparse
import time
import scipy, math
from tqdm import *

import torch
from torch.autograd import Variable
from torch.utils.data import DataLoader
from torch.utils.data.sampler import WeightedRandomSampler

import torchvision
from torchvision.transforms import *

from tensorboardX import SummaryWriter

import models
from datasets import *
from transforms import *
from mixup import *
from federated_utils import getLenOfGradientVectorCuda, transListOfArraysToArraysCuda, getShapeListCuda,transCudaArrayWithShapeList

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
parser.add_argument("--clients", type = int, default = 5, help= 'number of clients')
parser.add_argument("--matrix-size", type = int, default = 1000, help= 'size of randomization matrix')
parser.add_argument("--train-dataset", type=str, default='datasets/speech_commands/train', help='path of train dataset')
parser.add_argument("--valid-dataset", type=str, default='datasets/speech_commands/valid', help='path of validation dataset')
parser.add_argument("--background-noise", type=str, default='datasets/speech_commands/train/_background_noise_', help='path of background noise')
parser.add_argument("--comment", type=str, default='', help='comment in tensorboard title')
parser.add_argument("--batch-size", type=int, default=128, help='batch size')
parser.add_argument("--dataload-workers-nums", type=int, default=6, help='number of workers for dataloader')
parser.add_argument("--weight-decay", type=float, default=1e-2, help='weight decay')
parser.add_argument("--optim", choices=['sgd', 'adam'], default='sgd', help='choices of optimization algorithms')
parser.add_argument("--learning-rate", type=float, default=1e-4, help='learning rate for optimization')
parser.add_argument("--lr-scheduler", choices=['plateau', 'step'], default='plateau', help='method to adjust learning rate')
parser.add_argument("--lr-scheduler-patience", type=int, default=5, help='lr scheduler plateau: Number of epochs with no improvement after which learning rate will be reduced')
parser.add_argument("--lr-scheduler-step-size", type=int, default=50, help='lr scheduler step: number of epochs of learning rate decay.')
parser.add_argument("--lr-scheduler-gamma", type=float, default=0.1, help='learning rate is multiplied by the gamma to decrease it')
parser.add_argument("--max-epochs", type=int, default=70, help='max number of epochs')
parser.add_argument("--resume", type=str, help='checkpoint file to resume')
parser.add_argument("--model", choices=models.available_models, default=models.available_models[0], help='model of NN')
parser.add_argument("--input", choices=['mel32'], default='mel32', help='input of NN')
parser.add_argument('--mixup', action='store_true', help='use mixup')
args = parser.parse_args()

use_gpu = torch.cuda.is_available()
use_gpu = 'True'
print('use_gpu', use_gpu)
print('num of clients', args.clients)
if use_gpu:
    torch.backends.cudnn.benchmark = True

n_mels = 32

def build_dataset(n_mels = n_mels, train_dataset = args.train_dataset, valid_dataset = args.valid_dataset, background_noise = args.background_noise):
    data_aug_transform = Compose([ChangeAmplitude(), ChangeSpeedAndPitchAudio(), FixAudioLength(), ToSTFT(), StretchAudioOnSTFT(), TimeshiftAudioOnSTFT(), FixSTFTDimension()])
    bg_dataset = BackgroundNoiseDataset(background_noise, data_aug_transform)
    add_bg_noise = AddBackgroundNoiseOnSTFT(bg_dataset)
    train_feature_transform = Compose([ToMelSpectrogramFromSTFT(n_mels=n_mels), DeleteSTFT(), ToTensor('mel_spectrogram', 'input')])
    train_dataset = SpeechCommandsDataset(train_dataset,
                                Compose([LoadAudio(),
                                         data_aug_transform,
                                         add_bg_noise,
                                         train_feature_transform]))

    valid_feature_transform = Compose([ToMelSpectrogram(n_mels=n_mels), ToTensor('mel_spectrogram', 'input')])
    valid_dataset = SpeechCommandsDataset(valid_dataset,
                                Compose([LoadAudio(),
                                         FixAudioLength(),
                                         valid_feature_transform]))
    return train_dataset, valid_dataset

def main():
    # 1. load dataset, train and valid
    train_dataset, valid_dataset = build_dataset(n_mels = n_mels, train_dataset = args.train_dataset, valid_dataset = args.valid_dataset, background_noise = args.background_noise)
    print('train ',len(train_dataset), 'val ', len(valid_dataset))

    weights = train_dataset.make_weights_for_balanced_classes()
    sampler = WeightedRandomSampler(weights, len(weights))
    train_dataloader = DataLoader(train_dataset, batch_size=args.batch_size, sampler=sampler,
                              pin_memory=use_gpu, num_workers=args.dataload_workers_nums)
    valid_dataloader = DataLoader(valid_dataset, batch_size=args.batch_size, shuffle=False,
                              pin_memory=use_gpu, num_workers=args.dataload_workers_nums)
    # a name used to save checkpoints etc.
    # 2. prepare the model, checkpoint
    full_name = '%s_%s_%s_bs%d_lr%.1e_wd%.1e' % (args.model, args.optim, args.lr_scheduler, args.batch_size, args.learning_rate, args.weight_decay)
    if args.comment:
        full_name = '%s_%s' % (full_name, args.comment)

    model = models.create_model(model_name=args.model, num_classes=len(CLASSES), in_channels=1)

    if use_gpu:
        model = torch.nn.DataParallel(model).cuda()

    criterion = torch.nn.CrossEntropyLoss()

    if args.optim == 'sgd':
        optimizer = torch.optim.SGD(model.parameters(), lr=args.learning_rate, momentum=0.9, weight_decay=args.weight_decay)
    else:
        optimizer = torch.optim.Adam(model.parameters(), lr=args.learning_rate, weight_decay=args.weight_decay)

    start_timestamp = int(time.time()*1000)
    start_epoch = 0
    best_accuracy = 0
    best_loss = 1e100
    global_step = 0

    if args.resume:
        print("resuming getShapeLista checkpoint '%s'" % args.resume)
        checkpoint = torch.load(args.resume)
        model.load_state_dict(checkpoint['state_dict'])
        model.float()
        optimizer.load_state_dict(checkpoint['optimizer'])

        best_accuracy = checkpoint.get('accuracy', best_accuracy)
        best_loss = checkpoint.get('loss', best_loss)
        start_epoch = checkpoint.get('epoch', start_epoch)
        global_step = checkpoint.get('step', global_step)

        del checkpoint  # reduce memory

    if args.lr_scheduler == 'plateau':
        lr_scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=args.lr_scheduler_patience, factor=args.lr_scheduler_gamma)
    else:
        lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=args.lr_scheduler_step_size, gamma=args.lr_scheduler_gamma, last_epoch=start_epoch-1)

    def get_lr():
        return optimizer.param_groups[0]['lr']

    writer = SummaryWriter(comment=('_speech_commands_' + full_name))

    #3. train and validation
    print("training %s for Google speech commands..." % args.model)
    since = time.time()
    #grad_client_list = [[]] * args.clients
    global_grad_sum = torch.zeros(84454500).cuda()
    for epoch in range(start_epoch, args.max_epochs):
        print("epoch %3d with lr=%.02e" % (epoch, get_lr()))
        phase = 'train'
        writer.add_scalar('%s/learning_rate' % phase,  get_lr(), epoch)

        model.train()  # Set model to training mode

        running_loss = 0.0
        it = 0
        correct = 0
        total = 0
        A = 0
        #federated
        n = 0 # the length of squeezed gradient vector
        shape_list = []
        num_paddings = 0
        A_inv = 0
        S_i = 0
        S_j = 0
        u = 0
        sigma = 0
        vh = 0
        ns = 0

        #compute for each client
        current_client = 0
        pbar = tqdm(train_dataloader, unit="audios", unit_scale=train_dataloader.batch_size, disable=True)
        for batch in pbar:

            inputs = batch['input']
            inputs = torch.unsqueeze(inputs, 1)
            targets = batch['target']
            #print(inputs.shape, targets.shape)
            if args.mixup:
                inputs, targets = mixup(inputs, targets, num_classes=len(CLASSES))

            inputs = Variable(inputs, requires_grad=True)
            targets = Variable(targets, requires_grad=False)
            if use_gpu:
                inputs = inputs.cuda()
                targets = targets.cuda(async=True)

            outputs = model(inputs)
            if args.mixup:
                loss = mixup_cross_entropy_loss(outputs, targets)
            else:
                loss = criterion(outputs, targets)
            optimizer.zero_grad()
            loss.backward()
            #generate gradient list
            current_client_grad = []
            for name, param in model.named_parameters():
                if param.requires_grad:
                    #print(name, param.grad.shape, param.grad.type())#, param.grad)
                    current_client_grad.append(param.grad)
            #break
            #print(len(current_client_grad), current_client_grad[0].shape, current_client_grad[-1].shape)
            #randomize the gradient, if in a new batch, generate the randomization matrix
            if (current_client == 0):
                # Generate matrix A
                # 1. first obtain n(the total length of gradient vector) (if n == 0, get it, or pass)
                
                n = getLenOfGradientVectorCuda(current_client_grad)
                shape_list = getShapeListCuda(current_client_grad)
                print("gradient vector of length", n)
                # 2. randomize a full rank matrix A, the elements are evenly distributed
                #A = np.random.randint(0, 10000,size = (n, n)) 
                # Memory is not enough to create such a large matrix : CURRENT SOLUTION use a small size matrix and 
                # iterate over the vector
                A = torch.randint(0, 10000, (args.matrix_size, args.matrix_size))
                print("generating randomization matrix")
                A_inv = A.inverse()
                # two index set
                S_i = random.sample(range(0, 3*args.matrix_size), args.matrix_size)
                S_i.sort()
                S_j = random.sample(range(0, 2*args.matrix_size), args.matrix_size)
                S_j.sort()
                # extend to B
                B = torch.zeros(args.matrix_size, 3*args.matrix_size)
                for j in range(0, args.matrix_size):
                    for i in range(0, args.matrix_size):
                        B[i][S_i[j]] = A[i][j]
                C = torch.randint(0, 10000, (2 * args.matrix_size, 3*args.matrix_size))
                for i in range(0, args.matrix_size):
                    for j in range(0, 3*args.matrix_size):
                        C[S_j[i]][j] = B[i][j]
                #Does cuda speed up our calculation
                C = C.cuda()
                # SVD
                u, s, vh = torch.svd(C, some=False)
                print('u', u.shape, 's', s.shape, 'vh', vh.shape)
                # recontruct sigma
                sigma = torch.zeros(C.shape[0], C.shape[1]).cuda()
                #print(C.shape[0], C.shape[1])
                sigma[:min(C.shape[0],C.shape[1]), :min(C.shape[0],C.shape[1])] = s.diag()
                #assert(torch.equal(torch.mm(u, torch.mm(sigma, vh)), C))
                # C = torch.mm(torch.mm(u, sigma), vh.t())
                
                # linear independent group(null space)
                ns = scipy.linalg.null_space(C) # (3000, 1000) we use the first args.
                ns = torch.from_numpy(ns).cuda()
                print('ns', ns.shape)
            # do the randomization, obtain a new gradient vector for gradient_client_list[current_client]
            flatterned_grad = transListOfArraysToArraysCuda(current_client_grad, n)
            ###TODO: 1. need padding, 2. how to recover
            # random numbers
            r = torch.randint(0, 10000, (args.matrix_size, 1)).cuda()
            r_new =  torch.zeros(3 * args.matrix_size, 1).cuda()
            for i in range(args.matrix_size):
                r_new += r[i] * ns[:,i:i+1]
            num_paddings = math.ceil(float(n)/args.matrix_size) * args.matrix_size  - n
            print('flatterned', n, 'need padding', num_paddings) #np.array
            # extent to 2n
            flatterned_grad_extended = torch.zeros(n + num_paddings).cuda()
            flatterned_grad_extended[:n] = flatterned_grad
            #print(flatterned_grad_extended[:20])
            current_client_grad_after_random = torch.zeros(3*flatterned_grad_extended.shape[0]).cuda()
            new_grad = torch.randint(0, 10000, (3 * args.matrix_size, 1)).cuda()
            for i in range(0, flatterned_grad_extended.shape[0], args.matrix_size):
                if (i/args.matrix_size % 5000 == 0):
                    print(i/args.matrix_size, flatterned_grad_extended.shape[0] / args.matrix_size)
                for j in range(args.matrix_size):
                    new_grad[S_i[j]] = flatterned_grad_extended[i + j]
                
                # compute the randomize gradient
                randomized_gradient = torch.mm(vh.t(), new_grad + r_new)
                #print("randomized gradient", randomized_gradient.shape)
                
                
                current_client_grad_after_random[3*i : 3*i + 3*args.matrix_size] = torch.squeeze(randomized_gradient)
            print('after randomization', current_client_grad_after_random.shape)
            # transform the flatterned vector to matrix for model update
            if (current_client == args.clients - 1):
                print("client", current_client)
                global_grad_sum += current_client_grad_after_random
                # collect all the randomized gradient, cacluate the sum, and send to all clients
                # each client eliminate the randomness, and update the parameters according to the gradients
                # remove randomness
                res = torch.zeros(int(global_grad_sum.shape[0]/3)).cuda()
                alpha = torch.zeros(args.matrix_size, 1).cuda()
                for i in range(0, global_grad_sum.shape[0] , 3 * args.matrix_size):
                    tmp = torch.mm(torch.mm(u, sigma), global_grad_sum[i : i + 3*args.matrix_size])
                    for j in range(args.matrix_size): 
                        alpha[j] = tmp[S_j[j]]
                    res[int(i/3) : int(i/3) + args.matrix_size] = (torch.mm(A_inv, alpha)).squeeze()
                
                # set the gradient manually and update
                ### TODO
                recovered_grad_in_cuda = transCudaArrayWithShapeList(res, shape_list)
                ind = 0
                #print(recovered_grad_in_cuda, recovered_grad_in_cuda[0].shape, r)
                for name, param in model.named_parameters():
                    if param.requires_grad:
                        #print(recovered_grad_in_cuda[ind].shape, recovered_grad_in_cuda[ind].type())
                        print(recovered_grad_in_cuda[ind][:10])
                        param.grad = recovered_grad_in_cuda[ind]
                        ind+=1
                assert(ind == len(recovered_grad_in_cuda))
                optimizer.step()
                print("all clients finished")
                current_client = 0
                global_grad_sum = np.zeros((84454500))
            else :
                print("client", current_client)
                global_grad_sum += current_client_grad_after_random
                current_client += 1

            # only update the parameters when current_client == args.clients - 1

            # statistics
            it += 1
            global_step += 1
            #running_loss += loss.data[0]
            running_loss += loss.item()
            pred = outputs.data.max(1, keepdim=True)[1]
            if args.mixup:
                targets = batch['target']
                targets = Variable(targets, requires_grad=False).cuda(async=True)
            correct += pred.eq(targets.data.view_as(pred)).sum()
            total += targets.size(0)

            writer.add_scalar('%s/loss' % phase, loss.item(), global_step)

            # update the progress bar
            pbar.set_postfix({
                'loss': "%.05f" % (running_loss / it),
                'acc': "%.02f%%" % (100*float(correct)/total)
            })
            print("loss\t ", running_loss / it, "\t acc \t", 100*float(correct)/total)
            #break

        accuracy = float(correct)/total
        epoch_loss = running_loss / it
        writer.add_scalar('%s/accuracy' % phase, 100*accuracy, epoch)
        writer.add_scalar('%s/epoch_loss' % phase, epoch_loss, epoch)

if __name__ == '__main__':
    main()