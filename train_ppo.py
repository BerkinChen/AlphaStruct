import json
import os
import argparse
import torch
from typing import Optional
from datetime import datetime

import numpy as np
from sb3_contrib.ppo_mask import MaskablePPO
from stable_baselines3.common.callbacks import BaseCallback
from alphastruct.alphagen.data.calculator import AlphaCalculator

from alphastruct.alphagen.data.expression import *
from alphastruct.alphagen.models.alpha_pool import AlphaPool, AlphaPoolBase
from alphastruct.alphagen.rl.env.wrapper import AlphaEnv
from alphastruct.alphagen.rl.policy import LSTMSharedNet
from alphastruct.alphagen.utils.random import reseed_everything
from alphastruct.alphagen.rl.env.core import AlphaEnvCore
from alphastruct.alphagen_qlib.calculator import QLibStockDataCalculator


class CustomCallback(BaseCallback):
    def __init__(self,
                 save_freq: int,
                 show_freq: int,
                 save_path: str,
                 valid_calculator: AlphaCalculator,
                 test_calculator: AlphaCalculator,
                 name_prefix: str = 'rl_model',
                 timestamp: Optional[str] = None,
                 verbose: int = 0):
        super().__init__(verbose)
        self.save_freq = save_freq
        self.show_freq = show_freq
        self.save_path = save_path
        self.name_prefix = name_prefix

        self.valid_calculator = valid_calculator
        self.test_calculator = test_calculator

        if timestamp is None:
            self.timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
        else:
            self.timestamp = timestamp

    def _init_callback(self) -> None:
        if self.save_path is not None:
            os.makedirs(self.save_path, exist_ok=True)

    def _on_step(self) -> bool:
        return True

    def _on_rollout_end(self) -> None:
        assert self.logger is not None
        self.logger.record('pool/size', self.pool.size)
        self.logger.record('pool/significant', (np.abs(self.pool.weights[:self.pool.size]) > 1e-4).sum())
        self.logger.record('pool/best_ic_ret', self.pool.best_ic_ret)
        self.logger.record('pool/eval_cnt', self.pool.eval_cnt)
        ic_test = self.pool.test_ensemble(self.test_calculator)
        self.logger.record('test/ic', ic_test)
        self.save_checkpoint()

    def save_checkpoint(self):
        path = os.path.join(self.save_path, f'{self.name_prefix}_{self.timestamp}', f'{self.num_timesteps}_steps')
        self.model.save(path)   # type: ignore
        if self.verbose > 1:
            print(f'Saving model checkpoint to {path}')
        with open(f'{path}_pool.json', 'w') as f:
            json.dump(self.pool.to_dict(), f)

    def show_pool_state(self):
        state = self.pool.state
        n = len(state['exprs'])
        print('---------------------------------------------')
        for i in range(n):
            weight = state['weights'][i]
            expr_str = str(state['exprs'][i])
            ic_ret = state['ics_ret'][i]
            print(f'> Alpha #{i}: {weight}, {expr_str}, {ic_ret}')
        print(f'>> Ensemble ic_ret: {state["best_ic_ret"]}')
        print('---------------------------------------------')

    @property
    def pool(self) -> AlphaPoolBase:
        return self.env_core.pool

    @property
    def env_core(self) -> AlphaEnvCore:
        return self.training_env.envs[0].unwrapped  # type: ignore


def run(args):
    reseed_everything(args.seed)

    device = torch.device('cuda')
    close = Feature(FeatureType.CLOSE)
    target = Ref(close, -20) / close - 1

    # You can re-implement AlphaCalculator instead of using QLibStockDataCalculator.
    data_train = StockData(instrument=args.instruments,
                           start_time='2019-01-01',
                           end_time='2019-12-31')
    data_valid = StockData(instrument=args.instruments,
                           start_time='2020-01-01',
                           end_time='2020-12-31')
    data_test = StockData(instrument=args.instruments,
                          start_time='2021-01-01',
                          end_time='2022-12-31')
    calculator_train = QLibStockDataCalculator(data_train, target)
    calculator_valid = QLibStockDataCalculator(data_valid, target)
    calculator_test = QLibStockDataCalculator(data_test, target)

    pool = AlphaPool(
        capacity=args.pool,
        calculator=calculator_train,
        ic_lower_bound=None,
        l1_alpha=5e-3
    )
    env = AlphaEnv(pool=pool, device=device, print_expr=True)

    name_prefix = f"ppo_{args.instruments}_{args.pool}_{args.seed}"
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')

    # Create log directory
    log_dir = os.path.join('data/ppo_logs',
                           f"pool_{args.pool}",
                           f"{name_prefix}-{timestamp}")

    checkpoint_callback = CustomCallback(
        save_freq=10000,
        show_freq=10000,
        save_path=log_dir,
        valid_calculator=calculator_valid,
        test_calculator=calculator_test,
        name_prefix=name_prefix,
        timestamp=timestamp,
        verbose=1,
    )

    model = MaskablePPO(
        'MlpPolicy',
        env,
        policy_kwargs=dict(
            features_extractor_class=LSTMSharedNet,
            features_extractor_kwargs=dict(
                n_layers=2,
                d_model=128,
                dropout=0.1,
                device=device,
            ),
        ),
        gamma=1.,
        ent_coef=0.01,
        batch_size=128,
        tensorboard_log=log_dir,
        device=device,
        verbose=1,
    )
    model.learn(
        total_timesteps=args.steps,
        callback=checkpoint_callback,
        tb_log_name=f'{name_prefix}_{timestamp}',
    )


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--instruments', type=str, default='csi300')
    parser.add_argument('--pool', type=int, default=10)
    parser.add_argument('--steps', type=int, default=200_000)
    args = parser.parse_args()
    run(args)