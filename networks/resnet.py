from __future__ import annotations

import argparse
import os
import random
import shutil
import time
from enum import Enum

import torch.backends.cudnn as cudnn
import torch.distributed as dist
import torch.nn as nn
import torch.nn.functional as F
import torch.optim
import torch.utils.data.distributed
import torchvision.models as models
from PIL import ImageFile
from torch.optim.lr_scheduler import ReduceLROnPlateau
from torch.optim.lr_scheduler import StepLR
from torch.utils.data import Subset

ImageFile.LOAD_TRUNCATED_IMAGES = True

import dlrt

import comm
import datasets as dsets
from rich import print as rprint
from rich.columns import Columns

from rich.console import Console
console = Console(width=140)

# import cProfile, pstats, io
# from pstats import SortKey
# pr = cProfile.Profile()

import mlflow
import mlflow.pytorch
import random

class ToyNet(nn.Module):
    def __init__(self):
        super().__init__()
        # self.conv1 = nn.Conv2d(3, 6, 5)
        # self.pool = nn.MaxPool2d(2, 2)
        # self.conv2 = nn.Conv2d(6, 16, 5)
        self.fc0 = nn.Linear(3072, 16 * 5 * 5)
        # self.fc0a = nn.Linear(1000, 1000)
        # self.fc0b = nn.Linear(1000, 16 * 5 * 5)
        self.fc1 = nn.Linear(16 * 5 * 5, 120)
        # self.fc2 = nn.Linear(120, 84)
        self.fc3 = nn.Linear(120, 100)

    def forward(self, x):
        # x = self.pool(F.relu(self.conv1(x)))
        # x = self.pool(F.relu(self.conv2(x)))
        # x = torch.flatten(x, 1)  # flatten all dimensions except batch
        # x = F.relu(self.fc1(x))
        # # x = F.relu(self.fc2(x))
        # x = self.fc3(x)

        x = torch.flatten(x, 1)  # flatten all dimensions except batch
        x = F.relu(self.fc0(x))
        # x = F.relu(self.fc0a(x))
        # x = F.relu(self.fc0b(x))
        x = F.relu(self.fc1(x))
        # x = F.relu(self.fc2(x))
        x = self.fc3(x)
        return x


model_names = sorted(
    name
    for name in models.__dict__
    if name.islower() and not name.startswith("__") and callable(models.__dict__[name])
)

parser = argparse.ArgumentParser(description="PyTorch ImageNet Training")
parser.add_argument(
    "--data",
    metavar="DIR",
    nargs="?",
    default="imagenet",
    help="path to dataset (default: imagenet)",
)
parser.add_argument(
    "-a",
    "--arch",
    metavar="ARCH",
    default="resnet50",
    # choices=model_names,
    help="model architecture: " + " | ".join(model_names) + " (default: resnet18)",
)
parser.add_argument(
    "-j",
    "--workers",
    default=4,
    type=int,
    metavar="N",
    help="number of data loading workers (default: 4)",
)
parser.add_argument(
    "--epochs",
    default=90,
    type=int,
    metavar="N",
    help="number of total epochs to run",
)
parser.add_argument(
    "--start-epoch",
    default=0,
    type=int,
    metavar="N",
    help="manual epoch number (useful on restarts)",
)
parser.add_argument(
    "-b",
    "--batch-size",
    default=256,
    type=int,
    metavar="N",
    help="mini-batch size (default: 256), this is the total "
    "batch size of all GPUs on the current node when "
    "using Data Parallel or Distributed Data Parallel",
)
parser.add_argument(
    "--lr",
    "--learning-rate",
    default=0.1,
    type=float,
    metavar="LR",
    help="initial learning rate",
    dest="lr",
)
parser.add_argument(
    "--momentum",
    default=0.9,
    type=float,
    metavar="M",
    help="momentum",
)
parser.add_argument(
    "--wd",
    "--weight-decay",
    default=1e-4,
    type=float,
    metavar="W",
    help="weight decay (default: 1e-4)",
    dest="weight_decay",
)
parser.add_argument(
    "-p",
    "--print-freq",
    default=10,
    type=int,
    metavar="N",
    help="print frequency (default: 10)",
)
parser.add_argument(
    "--resume",
    default="",
    type=str,
    metavar="PATH",
    help="path to latest checkpoint (default: none)",
)
parser.add_argument(
    "-e",
    "--evaluate",
    dest="evaluate",
    action="store_true",
    help="evaluate model on validation set",
)
parser.add_argument(
    "--pretrained",
    dest="pretrained",
    action="store_true",
    help="use pre-trained model",
)
parser.add_argument(
    "--seed",
    default=None,
    type=int,
    help="seed for initializing training. ",
)
parser.add_argument("--dummy", action="store_true", help="use fake data to benchmark")
parser.add_argument(
    "--adaptive",
    default=False,
    type=bool,
    help="use adaptive training?"
)

best_acc1 = 0


def main(args):  # noqa: C901
    # if args.seed is not None:
    random.seed(42)
    torch.manual_seed(42)

    # initialize the torch process group across all processes
    print("comm init")
    try:
        if int(os.environ["SLURM_NTASKS"]) > 1:
            comm.init(method="nccl-slurm")
            args.world_size = dist.get_world_size()
            args.rank = dist.get_rank()
        else:
            args.world_size = 1
            args.rank = 0
    except KeyError:
        args.world_size = 1
        args.rank = 0

    mlflow.log_params({"world_size": args.world_size, "rank": args.rank})

    # create model
    if args.arch == "toynet":
        model = ToyNet()
    elif args.pretrained:
        print(f"=> using pre-trained model '{args.arch}'")
        model = models.__dict__[args.arch](pretrained=True)
    else:
        print(f"=> creating model '{args.arch}'")
        model = models.__dict__[args.arch]()

    # For multiprocessing distributed, DistributedDataParallel constructor
    # should always set the single device scope, otherwise,
    # DistributedDataParallel will use all available devices.
    if dist.is_initialized():
        args.gpu = dist.get_rank() % torch.cuda.device_count()  # only 4 gpus/node
        print(args.gpu)
    else:
        args.gpu = 0
    torch.cuda.set_device(args.gpu)
    model.cuda(args.gpu)
    device = torch.device(f"cuda:{args.gpu}")

    # criterion = nn.CrossEntropyLoss().to(device)
    # optimizer = torch.optim.SGD(
    #     model.parameters(), args.lr,
    #     momentum=args.momentum,
    #     weight_decay=args.weight_decay
    # )
    # print("converting model to DLRT")
    # print(model)

    optimizer = "SGD"
    nesterov = True
    skip_adapt = False
    rank_percent = 0.5
    eps_linear = 0.01
    eps_conv = 0.01
    mlflow.log_params(
        {
            "optimizer": optimizer,
            "nesterov": nesterov,
            "lr": args.lr,
            "momentum": args.momentum,
            "weight_decay": args.weight_decay,
            "skip_adapt": skip_adapt,
            "rank_percent": rank_percent,
            "loss fn": "CrossEntropy",
            "adaptive": args.adaptive,
            "eps_linear": eps_linear,
            "eps_conv": eps_conv,
        }
    )

    dlrt_trainer = dlrt.DLRTTrainer(
        torch_model=model,
        optimizer_name=optimizer,
        optimizer_kwargs={
            "lr": args.lr,
            "momentum": args.momentum,
            "weight_decay": args.weight_decay,
            'nesterov': nesterov,
        },
        adaptive=args.adaptive,
        criterion=nn.CrossEntropyLoss().to(device),
        init_ddp=dist.is_initialized(),
        mixed_precision=False,
        rank_percent=rank_percent,
        epsilon={"linear": eps_linear, "conv2d": eps_conv}
    )
    print(dlrt_trainer.model.model)
    if args.rank == 0:
        # print(dlrt_trainer.model.get_all_ranks())
        columns = Columns(dlrt_trainer.model.get_all_ranks(), equal=True, expand=True)
        rprint(columns)

    # print(dlrt_trainer)

    """Sets the learning rate to the initial LR decayed by 10 every 30 epochs"""
    scheduler = StepLR(dlrt_trainer.optimizer, step_size=30, gamma=0.1)

    # optionally resume from a checkpoint
    # TODO: add DLRT checkpointing
    if args.resume:
        if os.path.isfile(args.resume):
            print(f"=> loading checkpoint '{args.resume}'")
            if args.gpu is None:
                checkpoint = torch.load(args.resume)
            elif torch.cuda.is_available():
                # Map model to be loaded to specified single gpu.
                loc = f"cuda:{args.gpu}"
                checkpoint = torch.load(args.resume, map_location=loc)
            args.start_epoch = checkpoint["epoch"]
            best_acc1 = checkpoint["best_acc1"]
            if args.gpu is not None:
                # best_acc1 may be from a checkpoint from a different GPU
                best_acc1 = best_acc1.to(args.gpu)
            model.load_state_dict(checkpoint["state_dict"])
            # optimizer.load_state_dict(checkpoint["optimizer"])
            scheduler.load_state_dict(checkpoint["scheduler"])
            print(
                "=> loaded checkpoint '{}' (epoch {})".format(args.resume, checkpoint["epoch"]),
            )
        else:
            print(f"=> no checkpoint found at '{args.resume}'")

    # Data loading code
    # if args.dummy:
    #     print("=> Dummy data is used!")
    #     train_dataset = datasets.FakeData(1281167, (3, 224, 224), 1000, transforms.ToTensor())
    #     val_dataset = datasets.FakeData(50000, (3, 224, 224), 1000, transforms.ToTensor())
    # else:
    if os.environ["DATASET"] == "imagenet":
        dset_dict = dsets.get_imagenet_datasets(args.data, args.batch_size, args.workers)
    elif os.environ["DATASET"] == "cifar10":
        dset_dict = dsets.get_cifar10_datasets(args.data, args.batch_size, args.workers)
    else:
        raise NotImplementedError(f"Dataset {os.environ['DATASET']} not implemented")
    train_loader, train_sampler = dset_dict["train"]["loader"], dset_dict["train"]["sampler"]
    val_loader = dset_dict["val"]["loader"]

    if args.evaluate:
        validate(val_loader, dlrt_trainer, args)
        return

    for epoch in range(args.start_epoch, args.epochs):
        if dist.is_initialized():
            train_sampler.set_epoch(epoch)

        # train for one epoch
        # # profiling =====================
        # pr.enable()
        # # profiling =====================

        train_loss = train(train_loader, dlrt_trainer, epoch, device, args, skip_adapt=skip_adapt)
        # # profiling =====================
        # pr.disable()
        # s = io.StringIO()
        # sortby = SortKey.CUMULATIVE
        # ps = pstats.Stats(pr, stream=s).sort_stats(sortby)
        # ps.print_stats(15)
        # print(s.getvalue())
        # raise NotImplementedError
        # # profiling =====================

        # evaluate on validation set
        _ = validate(val_loader, dlrt_trainer, args)

        if isinstance(scheduler, ReduceLROnPlateau):
            scheduler.step(train_loss)
        else:  # StelLR / others
            scheduler.step()
        if args.rank == 0:
            print(dlrt_trainer.optimizer.param_groups[0]["lr"])


def train(train_loader, trainer: dlrt.DLRTTrainer, epoch, device, args, skip_adapt=True):
    batch_time = AverageMeter("Time", ":6.3f")
    data_time = AverageMeter("Data", ":6.3f")
    losses = AverageMeter("Loss", ":.4f")
    top1 = AverageMeter("Acc@1", ":6.2f")
    top5 = AverageMeter("Acc@5", ":6.2f")
    progress = ProgressMeter(
        len(train_loader),
        [batch_time, data_time, losses, top1, top5],
        prefix=f"Epoch: [{epoch}]",
    )

    # switch to train mode
    trainer.model.train()
    # rank = dist.get_rank()
    end = time.time()
    for i, (images, target) in enumerate(train_loader):
        # measure data loading time
        data_time.update(time.time() - end)

        # move data to the same device as model
        images = images.to(device, non_blocking=True)
        target = target.to(device, non_blocking=True)
        # console.rule(f"step {i}")
        koutput, loutput, soutput = trainer.train_step_new(
            images, target, skip_adapt=skip_adapt  # i < 100 and i % 50 != 0
        )
        # print(output.output.shape, target.shape)
        # argmax = torch.argmax(koutput.output, dim=1).to(torch.float32)
        # console.rule(f"train step {i}")
        # console.print(f"Argmax outputs k "
        #     f"mean: {argmax.mean().item():.5f}, max: {argmax.max().item():.5f}, "
        #     f"min: {argmax.min().item():.5f}, std: {argmax.std().item():.5f}"
        # )
        # argmax = torch.argmax(loutput.output, dim=1).to(torch.float32)
        # console.print(f"Argmax outputs l "
        #     f"mean: {argmax.mean().item():.5f}, max: {argmax.max().item():.5f}, "
        #     f"min: {argmax.min().item():.5f}, std: {argmax.std().item():.5f}"
        # )
        # argmax = torch.argmax(soutput.output, dim=1).to(torch.float32)
        # console.print(f"Argmax outputs s "
        #     f"mean: {argmax.mean().item():.5f}, max: {argmax.max().item():.5f}, "
        #     f"min: {argmax.min().item():.5f}, std: {argmax.std().item():.5f}"
        # )
        if torch.isnan(soutput.loss):
            raise ValueError("NaN loss")
        # measure accuracy and record loss
        acc1, acc5 = accuracy(soutput.output, target, topk=(1, 5))
        losses.update(soutput.loss.item(), images.size(0))
        top1.update(acc1[0], images.size(0))
        top5.update(acc5[0], images.size(0))

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()
        #if i == 2:
        #    raise RuntimeError("asdf")
        #    break

        if (i % args.print_freq == 0 or i == len(train_loader) - 1) and args.rank == 0:
            # console.rule(f"train step {i}")
            argmax = torch.argmax(koutput.output, dim=1).to(torch.float32)
            console.print(
                f"Argmax outputs k "
                f"mean: {argmax.mean().item():.5f}, max: {argmax.max().item():.5f}, "
                f"min: {argmax.min().item():.5f}, std: {argmax.std().item():.5f}"
            )
            argmax = torch.argmax(loutput.output, dim=1).to(torch.float32)
            console.print(
                f"Argmax outputs l "
                f"mean: {argmax.mean().item():.5f}, max: {argmax.max().item():.5f}, "
                f"min: {argmax.min().item():.5f}, std: {argmax.std().item():.5f}"
            )
            argmax = torch.argmax(soutput.output, dim=1).to(torch.float32)
            console.print(
                f"Argmax outputs s "
                f"mean: {argmax.mean().item():.5f}, max: {argmax.max().item():.5f}, "
                f"min: {argmax.min().item():.5f}, std: {argmax.std().item():.5f}"
            )
            progress.display(i + 1)
            mlflow.log_metrics(
                metrics={"train loss": losses.avg, "train top1": top1.avg.item(), "train top5": top5.avg.item()},
                step=trainer.counter,
            )
    if dist.is_initialized():
        losses.all_reduce()
    return losses.avg


def validate(val_loader, trainer: dlrt.DLRTTrainer, args):
    console.rule("validation")
    def run_validate(loader, base_progress=0):
        rank = 0 if not dist.is_initialized() else dist.get_rank()
        with torch.no_grad():
            end = time.time()
            num_elem = len(loader) - 1
            for i, (images, target) in enumerate(loader):
                i = base_progress + i
                images = images.cuda(args.gpu, non_blocking=True)
                target = target.cuda(args.gpu, non_blocking=True)

                # compute output
                output = trainer.valid_step(images, target)
                # argmax = torch.argmax(output.output, dim=1).to(torch.float32)
                # print(
                #     f"output mean: {argmax.mean().item()}, max: {argmax.max().item()}, min: {argmax.min().item()}, std: {argmax.std().item()}",
                # )

                # measure accuracy and record loss
                acc1, acc5 = accuracy(output.output, target, topk=(1, 5))
                losses.update(output.loss.item(), images.size(0))
                top1.update(acc1[0], images.size(0))
                top5.update(acc5[0], images.size(0))

                # measure elapsed time
                batch_time.update(time.time() - end)
                end = time.time()

                if (i % args.print_freq == 0 or i == num_elem) and rank == 0:
                    argmax = torch.argmax(output.output, dim=1).to(torch.float32)
                    print(
                        f"output mean: {argmax.mean().item()}, max: {argmax.max().item()}, min: {argmax.min().item()}, std: {argmax.std().item()}",
                    )
                    progress.display(i + 1)

    batch_time = AverageMeter("Time", ":6.3f", Summary.NONE)
    losses = AverageMeter("Loss", ":.4f", Summary.NONE)
    top1 = AverageMeter("Acc@1", ":6.2f", Summary.AVERAGE)
    top5 = AverageMeter("Acc@5", ":6.2f", Summary.AVERAGE)
    progress = ProgressMeter(
        len(val_loader) + (len(val_loader.sampler) * args.world_size < len(val_loader.dataset)),
        [batch_time, losses, top1, top5],
        prefix="Test: ",
    )

    # switch to evaluate mode
    trainer.model.eval()

    run_validate(val_loader)
    if dist.is_initialized():
        top1.all_reduce()
        top5.all_reduce()

    if len(val_loader.sampler) * args.world_size < len(val_loader.dataset):
        aux_val_dataset = Subset(
            val_loader.dataset,
            range(len(val_loader.sampler) * args.world_size, len(val_loader.dataset)),
        )
        aux_val_loader = torch.utils.data.DataLoader(
            aux_val_dataset,
            batch_size=args.batch_size,
            shuffle=False,
            num_workers=args.workers,
            pin_memory=True,
        )
        run_validate(aux_val_loader, len(val_loader))

    progress.display_summary()

    if dist.is_initialized():
        top1.all_reduce()
        top5.all_reduce()

    if args.rank == 0:
        mlflow.log_metrics(
            metrics={"val loss": losses.avg, "val top1": top1.avg.item(),
                     "val top5": top5.avg.item()},
            step=trainer.counter,
        )

    return top1.avg


def save_checkpoint(state, is_best, filename="checkpoint.pth.tar"):
    torch.save(state, filename)
    if is_best:
        shutil.copyfile(filename, "model_best.pth.tar")


class Summary(Enum):
    NONE = 0
    AVERAGE = 1
    SUM = 2
    COUNT = 3


class AverageMeter:
    """Computes and stores the average and current value"""

    def __init__(self, name, fmt=":f", summary_type=Summary.AVERAGE):
        self.name = name
        self.fmt = fmt
        self.summary_type = summary_type
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count

    def all_reduce(self):
        if torch.cuda.is_available():
            device = torch.device("cuda")
        elif torch.backends.mps.is_available():
            device = torch.device("mps")
        else:
            device = torch.device("cpu")
        total = torch.tensor([self.sum, self.count], dtype=torch.float32, device=device)
        dist.all_reduce(total, dist.ReduceOp.SUM, async_op=False)
        self.sum, self.count = total.tolist()
        self.avg = self.sum / self.count

    def __str__(self):
        fmtstr = "{name} {val" + self.fmt + "} ({avg" + self.fmt + "})"
        return fmtstr.format(**self.__dict__)

    def summary(self):
        fmtstr = ""
        if self.summary_type is Summary.NONE:
            fmtstr = ""
        elif self.summary_type is Summary.AVERAGE:
            fmtstr = "{name} {avg:.3f}"
        elif self.summary_type is Summary.SUM:
            fmtstr = "{name} {sum:.3f}"
        elif self.summary_type is Summary.COUNT:
            fmtstr = "{name} {count:.3f}"
        else:
            raise ValueError("invalid summary type %r" % self.summary_type)

        return fmtstr.format(**self.__dict__)


class ProgressMeter:
    def __init__(self, num_batches, meters, prefix=""):
        self.batch_fmtstr = self._get_batch_fmtstr(num_batches)
        self.meters = meters
        self.prefix = prefix
        self.rank = 0 if not dist.is_initialized() else dist.get_rank()

    def display(self, batch):
        entries = [self.prefix + self.batch_fmtstr.format(batch)]
        entries += [str(meter) for meter in self.meters]
        if self.rank == 0:
            # print("\t".join(entries))
            console.print(" ".join(entries))

    def display_summary(self):
        entries = [" *"]
        entries += [meter.summary() for meter in self.meters]
        if self.rank == 0:
            # print(" ".join(entries))
            console.print(" ".join(entries))

    def _get_batch_fmtstr(self, num_batches):
        num_digits = len(str(num_batches // 1))
        fmt = "{:" + str(num_digits) + "d}"
        return "[" + fmt + "/" + fmt.format(num_batches) + "]"


def accuracy(output, target, topk=(1,)):
    """Computes the accuracy over the k top predictions for the specified values of k"""
    with torch.no_grad():
        maxk = max(topk)
        batch_size = target.size(0)

        _, pred = output.topk(maxk, 1, True, True)
        pred = pred.t()
        correct = pred.eq(target.view(1, -1).expand_as(pred))

        res = []
        for k in topk:
            correct_k = correct[:k].reshape(-1).float().sum(0, keepdim=True)
            res.append(correct_k.mul_(100.0 / batch_size))
        return res


if __name__ == "__main__":
    args = parser.parse_args()
    print(args)
    mlflow.set_tracking_uri("file:/hkfs/work/workspace/scratch/qv2382-dlrt/mlflow/")
    experiment = mlflow.set_experiment(args.arch)
    # run_id -> adaptive needs to be unique, roll random int?
    run_name = f"wrapall-adapt-{args.adaptive}-{random.randint(1000000000, 9999999999)}"
    with mlflow.start_run():
        mlflow.set_tag("mlflow.runName", run_name)
        print("run_name:", run_name)
        print('tracking uri:', mlflow.get_tracking_uri())
        print('artifact uri:', mlflow.get_artifact_uri())
        main(args)
