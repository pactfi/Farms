from pyteal import Divw, Expr, If, Int, Seq, TealType
from pytealext import AutoLoadScratchVar, MulDiv64

from ..helpers import Addw, Mulw

UINT_MAX = 2**64 - 1


class RPTCalculatorResult:
    """The calculation result"""

    def __init__(self):
        self.rpt = AutoLoadScratchVar(TealType.uint64)
        self.rpt_frac = AutoLoadScratchVar(TealType.uint64)
        self.distributed_reward = AutoLoadScratchVar(TealType.uint64)


class RPTCalculator:
    """Calculate the RPT for a given reward distribution"""

    def __init__(self):
        self.result = RPTCalculatorResult()

    def run(
        self,
        dt: Expr,
        duration: Expr,
        rpt: Expr,
        rpt_frac: Expr,
        pending_reward: Expr,
        total_staked: Expr,
    ) -> Expr:
        """Execute RPT calculation with provided parameters

        Args:
            dt: time elapsed
            duration: duration of the reward period
            rpt: reward per token
            rpt_frac: fractional part of reward per token
            pending_reward: reward to be distributed over the course of duration
            total_staked: total amount of tokens staked
        """
        rpt_delta = AutoLoadScratchVar(TealType.uint64)
        rpt_delta_frac = AutoLoadScratchVar(TealType.uint64)

        carry = AutoLoadScratchVar(TealType.uint64)

        return Seq(
            Seq(
                self.result.distributed_reward.store(MulDiv64(pending_reward, dt, duration)),
                rpt_delta.store(self.result.distributed_reward / total_staked),
                # the 64 bit fractional part of rpt_delta
                rpt_delta_frac.store(
                    Divw(
                        self.result.distributed_reward % total_staked,
                        Int(0),
                        total_staked,
                    )
                ),
                Addw(
                    rpt_frac,
                    rpt_delta_frac,
                    result_high=carry.scratch_var,
                    result_low=self.result.rpt_frac.scratch_var,
                ),
                self.result.rpt.store(rpt + rpt_delta + carry),
            )
        )


class UserRewardCalculatorResult:
    """The calculation result"""

    def __init__(self):
        self.accrued_reward = AutoLoadScratchVar(TealType.uint64)


class UserRewardCalculator:
    """Calculate how many rewards should the user receive"""

    def __init__(self) -> None:
        self.result = UserRewardCalculatorResult()

    def run(
        self,
        rpt: Expr,
        rpt_frac: Expr,
        user_rpt: Expr,
        user_rpt_frac: Expr,
        user_staked: Expr,
    ) -> Expr:
        rpt_delta = AutoLoadScratchVar(TealType.uint64)
        rpt_delta_frac = AutoLoadScratchVar(TealType.uint64)
        reward_carry = AutoLoadScratchVar(TealType.uint64)
        return Seq(
            # rpt_delta = rpt - user_rpt
            # subtract the user's RPT from the global RPT
            If(user_rpt_frac <= rpt_frac)
            .Then(
                rpt_delta_frac.store(rpt_frac - user_rpt_frac),
                rpt_delta.store(rpt - user_rpt),
            )
            .Else(
                # subtract with borrow
                rpt_delta_frac.store(Int(UINT_MAX) - user_rpt_frac + rpt_frac + Int(1)),
                rpt_delta.store(rpt - user_rpt - Int(1)),
            ),
            Mulw(user_staked, rpt_delta_frac, result_high=reward_carry.scratch_var),
            self.result.accrued_reward.store(user_staked * rpt_delta + reward_carry),
        )
