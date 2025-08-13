import os
import yaml
import argparse
import torch
from datetime import datetime

from alphastruct.fqf_iqn_qrdqn.agent import QRQCMAgent, IQCMAgent, FQCMAgent
from alphastruct.alphagen.data.expression import Feature, FeatureType, Ref, StockData
from alphastruct.alphagen_qlib.calculator import QLibStockDataCalculator
from alphastruct.alphagen.models.alpha_pool import AlphaPool
from alphastruct.alphagen.rl.env.wrapper import AlphaEnv


def run(args):

    # torch.cuda.set_device(args.cuda)
    config_path = os.path.join('config/qcm_config', f'{args.model}.yaml')

    with open(config_path) as f:
        config = yaml.load(f, Loader=yaml.SafeLoader)

    # Create environments.
    device = torch.device(f'cuda')
    close = Feature(FeatureType.CLOSE)
    target = Ref(close, -20) / close - 1
    instruments: float = 'all'

    data_train = StockData(instrument=instruments,
                           start_time='2010-01-01',
                           end_time='2019-12-31')
    data_valid = StockData(instrument=instruments,
                           start_time='2020-01-01',
                           end_time='2020-12-31')
    data_test = StockData(instrument=instruments,
                          start_time='2021-01-01',
                          end_time='2022-12-31')
    train_calculator = QLibStockDataCalculator(data_train, target)
    valid_calculator = QLibStockDataCalculator(data_valid, target)
    test_calculator = QLibStockDataCalculator(data_test, target)
    train_pool = AlphaPool(capacity=args.pool,
                           calculator=train_calculator,
                           ic_lower_bound=None,
                           l1_alpha=5e-3)
    train_env = AlphaEnv(pool=train_pool, device=device, print_expr=True)

    # Specify the directory to log.
    name = args.model
    time = datetime.now().strftime("%Y%m%d-%H%M")
    if name in ['qrdqn', 'iqn']:
        log_dir = os.path.join('data/alpha_logs',
                           f'pool_{args.pool}_QCM_{args.std_lam}',
                           f"{name}-seed{args.seed}-{time}-N{config['N']}-lr{config['lr']}-per{config['use_per']}-gamma{config['gamma']}-step{config['multi_step']}")
    elif name == 'fqf':
        log_dir = os.path.join('data/alpha_logs',
                           f'pool_{args.pool}_QCM_{args.std_lam}',
                           f"{name}-seed{args.seed}-{time}-N{config['N']}-lr{config['quantile_lr']}-per{config['use_per']}-gamma{config['gamma']}-step{config['multi_step']}")

    # Create the agent and run.
    if name == 'qrdqn':
        agent = QRQCMAgent(env=train_env,
                           valid_calculator=valid_calculator,
                           test_calculator=test_calculator,
                           log_dir=log_dir,
                           seed=args.seed,
                           std_lam=args.std_lam,
                           cuda=True,
                           **config)
    elif name == 'iqn':
        agent = IQCMAgent(env=train_env,
                          valid_calculator=valid_calculator,
                          test_calculator=test_calculator,
                          log_dir=log_dir,
                          seed=args.seed,
                          std_lam=args.std_lam,
                          cuda=True, **config)
    elif name == 'fqf':
        agent = FQCMAgent(env=train_env,
                          valid_calculator=valid_calculator,
                          test_calculator=test_calculator,
                          log_dir=log_dir,
                          seed=args.seed,
                          std_lam=args.std_lam,
                          cuda=True, **config)
    agent.run()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', type=str, default='qrdqn')
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--pool', type=int, default=20)
    parser.add_argument('--std-lam', type=float, default=1.0)
    args = parser.parse_args()
    run(args)
