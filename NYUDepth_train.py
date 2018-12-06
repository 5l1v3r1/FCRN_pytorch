# -*- coding: utf-8 -*-
# @Time    : 2018/10/23 19:40
# @Author  : Wang Xin
# @Email   : wangxin_buaa@163.com
from datetime import datetime
import shutil
import socket
import time
import torch
from tensorboardX import SummaryWriter

import FCRN
from dataloaders import nyu_dataloader
from metrics import AverageMeter, Result
import utils
import criteria
import os
import torch.nn as nn

# 切换成单卡
# os.environ["CUDA_VISIBLE_DEVICES"] = "1"  # 默认使用GPU 0

args = utils.parse_command()
print(args)

best_result = Result()
best_result.set_to_worst()


def NYUDepth_loader(data_path, batch_size=32, isTrain=True):
    if isTrain:
        traindir = os.path.join(data_path, 'train')
        print(traindir)

        if os.path.exists(traindir):
            print('训练集目录存在')
        trainset = nyu_dataloader.NYUDataset(traindir, type='train')
        train_loader = torch.utils.data.DataLoader(
            trainset, batch_size=batch_size, shuffle=True)  # @wx 多线程读取失败
        return train_loader
    else:
        valdir = os.path.join(data_path, 'val')
        print(valdir)

        if os.path.exists(valdir):
            print('测试集目录存在')
        valset = nyu_dataloader.NYUDataset(valdir, type='val')
        val_loader = torch.utils.data.DataLoader(
            valset, batch_size=1, shuffle=False  # shuffle 测试时是否设置成False batch_size 恒定为1
        )
        return val_loader


def main():
    global args, best_result, output_directory

    if torch.cuda.device_count() > 1:
        # args.batch_size = args.batch_size * torch.cuda.device_count()
        print("Let's use", torch.cuda.device_count(), "GPUs!")
        train_loader = NYUDepth_loader(args.data_path, batch_size=args.batch_size * torch.cuda.device_count(),
                                       isTrain=True)
        val_loader = NYUDepth_loader(args.data_path, batch_size=args.batch_size, isTrain=False)
    else:
        print("Let's use", torch.cuda.current_device())
        train_loader = NYUDepth_loader(args.data_path, batch_size=args.batch_size, isTrain=True)
        val_loader = NYUDepth_loader(args.data_path, isTrain=False)

    if args.resume:
        assert os.path.isfile(args.resume), \
            "=> no checkpoint found at '{}'".format(args.resume)
        print("=> loading checkpoint '{}'".format(args.resume))
        checkpoint = torch.load(args.resume)

        start_epoch = checkpoint['epoch'] + 1
        best_result = checkpoint['best_result']
        if torch.cuda.device_count() > 1:
            model_dict = checkpoint['model'].module.state_dict()  # 如果是多卡训练的要加module
        else:
            model_dict = checkpoint['model'].state_dict()
        # model_dict = checkpoint['model'].state_dict()
        model = FCRN.ResNet(layers=50, output_size=((228, 304)))
        model.load_state_dict(model_dict)
        # 使用SGD进行优化
        optimizer = torch.optim.SGD(model.parameters(), lr=args.lr, momentum=args.momentum)

        print("=> loaded checkpoint (epoch {})".format(checkpoint['epoch']))
    else:
        print("=> creating Model")
        model = FCRN.ResNet(layers=50, output_size=((228, 304)), pretrained=True)
        print("=> model created.")
        optimizer = torch.optim.SGD(model.parameters(), lr=args.lr, momentum=args.momentum)
        start_epoch = 0
    # 如果有多GPU 使用多GPU训练
    if torch.cuda.device_count() > 1:
        model = nn.DataParallel(model)

    model = model.cuda()

    # 定义loss函数
    criterion = criteria.MaskedL1Loss().cuda()

    # 创建保存结果目录文件
    output_directory = utils.get_output_directory(args)
    if not os.path.exists(output_directory):
        os.makedirs(output_directory)
    best_txt = os.path.join(output_directory, 'best.txt')

    log_path = os.path.join(output_directory, 'logs',
                            datetime.now().strftime('%b%d_%H-%M-%S') + '_' + socket.gethostname())
    if os.path.isdir(log_path):
        shutil.rmtree(log_path)
    os.makedirs(log_path)
    logger = SummaryWriter(log_path)

    for epoch in range(start_epoch, args.epochs):
        lr = utils.adjust_learning_rate(optimizer, args.lr, epoch)  # 更新学习率

        train(train_loader, model, criterion, optimizer, epoch, logger)  # train for one epoch
        result, img_merge = validate(val_loader, model, epoch, logger)  # evaluate on validation set

        # remember best rmse and save checkpoint
        is_best = result.rmse < best_result.rmse
        if is_best:
            best_result = result
            with open(best_txt, 'w') as txtfile:
                txtfile.write(
                    "epoch={}\nrmse={:.3f}\nrml={:.3f}\nlog10={:.3f}\nd1={:.3f}\nd2={:.3f}\nd3={:.3f}\nt_gpu={:.4f}\n".
                        format(epoch, result.rmse, result.absrel, result.lg10, result.delta1, result.delta2,
                               result.delta3,
                               result.gpu_time))
            if img_merge is not None:
                img_filename = output_directory + '/comparison_best.png'
                utils.save_image(img_merge, img_filename)

        # 每个Epoch都保存解雇
        utils.save_checkpoint({
            'args': args,
            'epoch': epoch,
            'model': model,
            'best_result': best_result,
            'optimizer': optimizer,
        }, is_best, epoch, output_directory)

    logger.close()


# 在NYUDepth数据集上训练
def train(train_loader, model, criterion, optimizer, epoch, logger):
    average_meter = AverageMeter()
    model.train()  # switch to train mode
    end = time.time()

    batch_num = len(train_loader)

    for i, (input, target) in enumerate(train_loader):

        # itr_count += 1
        input, target = input.cuda(), target.cuda()
        # print('input size  = ', input.size())
        # print('target size = ', target.size())
        torch.cuda.synchronize()
        data_time = time.time() - end

        # compute pred
        end = time.time()
        pred = model(input)  # @wx 注意输出

        # print('pred size = ', pred.size())
        # print('target size = ', target.size())

        loss = criterion(pred, target)
        optimizer.zero_grad()
        loss.backward()  # compute gradient and do SGD step
        optimizer.step()
        torch.cuda.synchronize()
        gpu_time = time.time() - end

        # measure accuracy and record loss
        result = Result()
        result.evaluate(pred.data, target.data)
        average_meter.update(result, gpu_time, data_time, input.size(0))
        end = time.time()

        if (i + 1) % args.print_freq == 0:
            print('=> output: {}'.format(output_directory))
            print('Train Epoch: {0} [{1}/{2}]\t'
                  't_Data={data_time:.3f}({average.data_time:.3f}) '
                  't_GPU={gpu_time:.3f}({average.gpu_time:.3f})\n\t'
                  'RMSE={result.rmse:.2f}({average.rmse:.2f}) '
                  'RML={result.absrel:.2f}({average.absrel:.2f}) '
                  'Log10={result.lg10:.3f}({average.lg10:.3f}) '
                  'Delta1={result.delta1:.3f}({average.delta1:.3f}) '
                  'Delta2={result.delta2:.3f}({average.delta2:.3f}) '
                  'Delta3={result.delta3:.3f}({average.delta3:.3f})'.format(
                epoch, i + 1, len(train_loader), data_time=data_time,
                gpu_time=gpu_time, result=result, average=average_meter.average()))
            current_step = epoch * batch_num + i
            logger.add_scalar('Train/RMSE', result.rmse, current_step)
            logger.add_scalar('Train/rml', result.absrel, current_step)
            logger.add_scalar('Train/Log10', result.lg10, current_step)
            logger.add_scalar('Train/Delta1', result.delta1, current_step)
            logger.add_scalar('Train/Delta2', result.delta2, current_step)
            logger.add_scalar('Train/Delta3', result.delta3, current_step)

    avg = average_meter.average()


# 修改
def validate(val_loader, model, epoch, logger, write_to_file=True):
    average_meter = AverageMeter()

    model.eval()  # switch to evaluate mode

    end = time.time()

    for i, (input, target) in enumerate(val_loader):

        input, target = input.cuda(), target.cuda()
        torch.cuda.synchronize()
        data_time = time.time() - end

        # compute output
        end = time.time()
        with torch.no_grad():
            pred = model(input)
        torch.cuda.synchronize()
        gpu_time = time.time() - end

        # measure accuracy and record loss
        result = Result()
        result.evaluate(pred.data, target.data)

        average_meter.update(result, gpu_time, data_time, input.size(0))
        end = time.time()

        # save 8 images for visualization
        skip = 50
        if args.modality == 'd':
            img_merge = None
        else:
            if args.modality == 'rgb':
                rgb = input
            elif args.modality == 'rgbd':
                rgb = input[:, :3, :, :]
                depth = input[:, 3:, :, :]

            if i == 0:
                if args.modality == 'rgbd':
                    img_merge = utils.merge_into_row_with_gt(rgb, depth, target, pred)
                else:
                    img_merge = utils.merge_into_row(rgb, target, pred)
            elif (i < 8 * skip) and (i % skip == 0):
                if args.modality == 'rgbd':
                    row = utils.merge_into_row_with_gt(rgb, depth, target, pred)
                else:
                    row = utils.merge_into_row(rgb, target, pred)
                img_merge = utils.add_row(img_merge, row)
            elif i == 8 * skip:
                filename = output_directory + '/comparison_' + str(epoch) + '.png'
                utils.save_image(img_merge, filename)

        if (i + 1) % args.print_freq == 0:
            print('Test: [{0}/{1}]\t'
                  't_GPU={gpu_time:.3f}({average.gpu_time:.3f})\n\t'
                  'RMSE={result.rmse:.2f}({average.rmse:.2f}) '
                  'RML={result.absrel:.2f}({average.absrel:.2f}) '
                  'Log10={result.lg10:.3f}({average.lg10:.3f}) '
                  'Delta1={result.delta1:.3f}({average.delta1:.3f}) '
                  'Delta2={result.delta2:.3f}({average.delta2:.3f}) '
                  'Delta3={result.delta3:.3f}({average.delta3:.3f})'.format(
                i + 1, len(val_loader), gpu_time=gpu_time, result=result, average=average_meter.average()))

    avg = average_meter.average()

    print('\n*\n'
          'RMSE={average.rmse:.3f}\n'
          'Rel={average.absrel:.3f}\n'
          'Log10={average.lg10:.3f}\n'
          'Delta1={average.delta1:.3f}\n'
          'Delta2={average.delta2:.3f}\n'
          'Delta3={average.delta3:.3f}\n'
          't_GPU={time:.3f}\n'.format(
        average=avg, time=avg.gpu_time))

    logger.add_scalar('Test/rmse', avg.rmse, epoch)
    logger.add_scalar('Test/Rel', avg.absrel, epoch)
    logger.add_scalar('Test/log10', avg.lg10, epoch)
    logger.add_scalar('Test/Delta1', avg.delta1, epoch)
    logger.add_scalar('Test/Delta2', avg.delta2, epoch)
    logger.add_scalar('Test/Delta3', avg.delta3, epoch)
    return avg, img_merge


if __name__ == '__main__':
    main()
