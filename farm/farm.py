from beaker import (
    AccountStateValue,
    Application,
    ApplicationStateValue,
    Authorize,
    clear_state,
    close_out,
    create,
    external,
    internal,
    opt_in,
    update,
)
from beaker.application import get_method_selector, get_method_signature
from pyteal import (
    AccountParam,
    And,
    App,
    Approve,
    Assert,
    AssetHolding,
    Balance,
    BoxGet,
    BoxPut,
    Bytes,
    BytesZero,
    Expr,
    For,
    Global,
    Gtxn,
    If,
    InnerTxnBuilder,
    Int,
    MinBalance,
    Not,
    OnComplete,
    ScratchVar,
    Seq,
    Subroutine,
    TealType,
    Txn,
    TxnField,
    TxnType,
    abi,
)
from pytealext import (
    AutoLoadScratchVar,
    MakeInnerApplicationCallTxn,
    MakeInnerAssetTransferTxn,
    Min,
    Uint64Array,
)
from pytealext.array import INDEX_NOT_FOUND, array_get, array_length

from ..gas_station import GasStationContract
from ..helpers import SendToAddress, get_deposited_amount, validate_transfer
from .escrow import ESCROW_HUSK_BYTECODE, GS_MASTER_KEY, EscrowPrecompile
from .rpt_calculator import RPTCalculator, UserRewardCalculator

MAX_FARMED_ASSETS = Int(7)
DEFAULT_ZERO_ARRAY = BytesZero(Int(MAX_FARMED_ASSETS.value * 8))
ESCROW_BOX_NAME = "Escrow"
CONTRACT_VERSION = 101


class Farm(Application):
    """Configurable farming protocol"""

    escrow = EscrowPrecompile()
    escrow_husk = Bytes(ESCROW_HUSK_BYTECODE)

    contract_name = ApplicationStateValue(
        stack_type=TealType.bytes,
        key=Bytes("CONTRACT_NAME"),
        default=Bytes("PACT FARM"),
        static=True,
        descr="Contract's name to easily distinguish different contract types on-chain",
    )

    contract_version = ApplicationStateValue(
        stack_type=TealType.uint64,
        key=Bytes("VERSION"),
        default=Int(CONTRACT_VERSION),
        static=False,
        descr="The contract's version encoded as XYY where X is major release, YY is minor release",
    )

    admin = ApplicationStateValue(
        stack_type=TealType.bytes,
        key=Bytes("Admin"),
        descr="The admin address",
    )

    updater = ApplicationStateValue(
        stack_type=TealType.bytes,
        key=Bytes("Updater"),
        descr="The address of an account that can update the contract",
    )

    total_staked = ApplicationStateValue(
        stack_type=TealType.uint64,
        key=Bytes("TotalStaked"),
        default=Int(0),
        descr="Total amount of staked tokens",
    )

    updated_at = ApplicationStateValue(
        TealType.uint64,
        Bytes("UpdatedAt"),
        Global.latest_timestamp(),
        descr="The last time the rewards were updated",
    )

    number_of_stakers = ApplicationStateValue(
        stack_type=TealType.uint64,
        key=Bytes("NumStakers"),
        default=Int(0),
        descr="The amount of accounts opted-in to the contract",
    )

    staked_asset_id = ApplicationStateValue(
        stack_type=TealType.uint64,
        key=Bytes("StakedAssetID"),
        static=True,
        descr="The ID of the stakable asset",
    )

    duration = ApplicationStateValue(
        stack_type=TealType.uint64,
        key=Bytes("Duration"),
        default=Int(0),
        descr="Remaining time to distribute pending rewards",
    )

    next_duration = ApplicationStateValue(
        stack_type=TealType.uint64,
        key=Bytes("NextDuration"),
        default=Int(0),
        descr="Time to distribute upcoming rewards",
    )

    rpt = ApplicationStateValue(
        stack_type=TealType.bytes,
        key=Bytes("RPT"),
        default=DEFAULT_ZERO_ARRAY,
        descr='The most recently updated "reward per token" of each farmed token',
    )

    rpt_frac = ApplicationStateValue(
        stack_type=TealType.bytes,
        key=Bytes("RPT_frac"),
        default=DEFAULT_ZERO_ARRAY,
        descr="The most recently updated RPT fraction of each farmed token",
    )

    next_rewards = ApplicationStateValue(
        stack_type=TealType.bytes,
        key=Bytes("NextRewards"),
        default=DEFAULT_ZERO_ARRAY,
        descr='Upcoming rewards after "duration"',
    )

    pending_rewards = ApplicationStateValue(
        TealType.bytes,
        Bytes("PendingRewards"),
        DEFAULT_ZERO_ARRAY,
        descr="The remaining rewards to be distributed over the course of the 'duration'",
    )

    total_rewards = ApplicationStateValue(
        TealType.bytes,
        Bytes("TotalRewards"),
        DEFAULT_ZERO_ARRAY,
        descr="Total distributed rewards throughout lifetime of this contract",
    )

    claimed_rewards = ApplicationStateValue(
        TealType.bytes,
        Bytes("ClaimedRewards"),
        DEFAULT_ZERO_ARRAY,
        descr="Total claimed rewards throughout lifetime of this contract",
    )

    reward_asset_ids = ApplicationStateValue(
        TealType.bytes,
        Bytes("RewardAssetIDs"),
        Bytes(""),
        descr="The IDs of the reward assets",
    )

    # user vars
    user_staked = AccountStateValue(
        stack_type=TealType.uint64,
        key=Bytes("Staked"),
        default=Int(0),
        descr="The user's staked tokens count",
    )

    user_escrow_ID = AccountStateValue(
        stack_type=TealType.uint64,
        key=Bytes("EscrowID"),
        descr="The user's Micro Farm Application ID",
    )

    user_rpt = AccountStateValue(
        stack_type=TealType.bytes,
        key=Bytes("RPT"),
        default=DEFAULT_ZERO_ARRAY,
        descr="The user's reward per token",
    )

    user_rpt_frac = AccountStateValue(
        stack_type=TealType.bytes,
        key=Bytes("RPT_frac"),
        default=DEFAULT_ZERO_ARRAY,
        descr="The user's RPT fraction",
    )

    user_claimed_rewards = AccountStateValue(
        stack_type=TealType.bytes,
        key=Bytes("ClaimedRewards"),
        default=DEFAULT_ZERO_ARRAY,
        descr="The user's rewards that have been claimed since opt-in",
    )

    user_accrued_rewards = AccountStateValue(
        stack_type=TealType.bytes,
        key=Bytes("AccruedRewards"),
        default=DEFAULT_ZERO_ARRAY,
        descr="The user's rewards that can be claimed",
    )

    def __init__(self, version: int = 8):
        super().__init__(version)

    @internal(TealType.uint64)
    def validate_escrow(self, escrow: abi.Application, local_state_owner: abi.Account) -> Expr:
        """Validates that the escrow has all the expected parameters

        Returns:
            Expression evaluating to 1 if the escrow is valid, 0 otherwise
        """
        return Seq(
            approval := escrow.params().approval_program(),
            creator := escrow.params().creator_address(),
            farm := App.globalGetEx(escrow.application_id(), Bytes(GS_MASTER_KEY)),
            And(
                # hasValue for escrow needs to be checked only once
                approval.hasValue(),
                approval.value() == self.escrow.approval.binary,
                creator.value() == local_state_owner.address(),
                farm.hasValue(),
                farm.value() == Global.current_application_id(),
                self.user_escrow_ID[local_state_owner.address()].get() == escrow.application_id(),
            ),
        )

    @create
    def create(
        self,
        gas_station: abi.Application,
        staked_asset: abi.Asset,
        admin: abi.Account,
        updater: abi.Account,
    ) -> Expr:
        return Seq(
            self.initialize_application_state(),
            self.staked_asset_id.set(staked_asset.asset_id()),
            self.admin.set(admin.address()),
            self.updater.set(updater.address()),
            # store the compiled escrow in a box. The box is not used by the contract, but it
            # makes the binary readily available offchain.
            # This requires a minimum balance, so we'll ask the gas station to pay for it
            InnerTxnBuilder.ExecuteMethodCall(
                app_id=gas_station.application_id(),
                method_signature=get_method_signature(GasStationContract.withdraw),
                args=[],
                extra_fields={TxnField.fee: Int(0)},
            ),
            BoxPut(Bytes(ESCROW_BOX_NAME), self.escrow.approval.binary),
        )

    @update(authorize=Authorize.only(updater))
    def update(self) -> Expr:
        post_update_txn = Gtxn[Txn.group_index() + Int(1)]
        return Seq(
            Assert(post_update_txn.on_completion() == OnComplete.NoOp),
            Assert(post_update_txn.application_id() == Global.current_application_id()),
            Assert(post_update_txn.application_args.length() == Int(1)),
            Assert(
                post_update_txn.application_args[0] == Bytes(get_method_selector(self.post_update))
            ),
        )

    @external
    def post_update(self) -> Expr:
        """Post-update migration logic"""
        return Seq(
            # Failsafe - check that bytecode of the escrow remains unchanged
            # in order to guarantee compatibility with previously deployed escrows
            (old_approval := BoxGet(Bytes(ESCROW_BOX_NAME))),
            Assert(
                old_approval.value() == self.escrow.approval.binary,
                comment="Escrow's bytecode must remain unchanged.",
            ),
            Assert(
                Int(CONTRACT_VERSION) > self.contract_version.get(),
                comment="Update was already done",
            ),
            # version specific migrations:
            # (If there is an important change then this must be done in the same group as update!)
            # ...
            # version update:
            self.contract_version.set(Int(CONTRACT_VERSION)),
        )

    @opt_in
    def opt_in(self) -> Expr:
        """User opts-in to the Farm contract and configures their Escrow

        Due to limitations in Ledger, the Farm injects the MicroFarm code into the clean contract.
        """
        prev_txn_id = AutoLoadScratchVar(TealType.uint64)
        created_app_id = AutoLoadScratchVar(TealType.uint64)
        return Seq(
            prev_txn_id.store(Txn.group_index() - Int(1)),
            created_app_id.store(Gtxn[prev_txn_id].created_application_id()),
            # Modify the bytecode of the created "microfarm"
            MakeInnerApplicationCallTxn(
                application_id=created_app_id,
                on_completion=OnComplete.UpdateApplication,
                approval_program=self.escrow.approval.binary,
                clear_state_program=self.escrow.clear.binary,
                fee=Int(0),
            ),
            # Validate that the escrow is being deployed correctly
            (master := App.globalGetEx(created_app_id, Bytes(GS_MASTER_KEY))),
            Assert(
                And(
                    Gtxn[prev_txn_id].type_enum() == TxnType.ApplicationCall,
                    Gtxn[prev_txn_id].application_id() == Int(0),
                    Gtxn[prev_txn_id].on_completion() == OnComplete.NoOp,
                    Gtxn[prev_txn_id].approval_program() == self.escrow_husk,
                    Gtxn[prev_txn_id].sender() == Txn.sender(),
                    # We don't care about the clear state program, because
                    # Escrow cannot be opted-in to
                ),
                comment="Previous transaction must be an Escrow creation",
            ),
            Assert(
                master.value() == Global.current_application_id(),
                comment="Escrow's referenced farm must be the current application",
            ),
            # Init user's local state
            self.initialize_account_state(),
            self.user_escrow_ID.set(created_app_id.load()),
        )

    @clear_state
    def clear_state(self) -> Expr:
        return Seq(
            self.exit_farm(),
            Approve(),
        )

    @close_out
    def close_out(self) -> Expr:
        return Seq(
            Assert(self.user_staked[Txn.sender()] == Int(0)),
            Assert(self.user_accrued_rewards[Txn.sender()] == DEFAULT_ZERO_ARRAY),
            self.exit_farm(),
            Approve(),
        )

    @external(authorize=Authorize.only(updater))
    def change_updater(self, new_updater: abi.Account) -> Expr:
        return self.updater.set(new_updater.address())

    @external(authorize=Authorize.only(admin))
    def change_admin(self, new_admin: abi.Account) -> Expr:
        return self.admin.set(new_admin.address())

    @internal
    def assert_algo_balance_is_sufficient(self) -> Expr:
        """Assert that Algo assets are sufficient to pay all upcoming rewards.

        This function never fails if Algo is not the reward asset
        """
        reward_asset_ids = Uint64Array()
        algos_index = AutoLoadScratchVar(TealType.uint64)
        total_algos = Balance(Global.current_application_address()) - MinBalance(
            Global.current_application_address()
        )
        total_required_algos = (
            array_get(self.total_rewards.get(), algos_index.load())
            + array_get(self.pending_rewards.get(), algos_index.load())
            + array_get(self.next_rewards.get(), algos_index.load())
            - array_get(self.claimed_rewards.get(), algos_index.load())
        )
        return Seq(
            reward_asset_ids.decode(self.reward_asset_ids.get()),
            algos_index.store(reward_asset_ids.index(Int(0))),
            If(algos_index != INDEX_NOT_FOUND).Then(
                Assert(
                    total_algos >= total_required_algos,
                    comment="You must deposit Algos for the opt-in",
                )
            ),
        )

    @external(authorize=Authorize.only(admin))
    def add_reward_asset(self, new_reward_asset: abi.Asset) -> Expr:
        new_reward_asset_id = new_reward_asset.asset_id()
        tmp_reward_asset_ids = Uint64Array()
        return Seq(
            tmp_reward_asset_ids.decode(self.reward_asset_ids.get()),
            Assert(tmp_reward_asset_ids.length() < MAX_FARMED_ASSETS),
            Assert(Not(tmp_reward_asset_ids.exists(new_reward_asset_id))),
            tmp_reward_asset_ids.append(new_reward_asset_id),
            self.reward_asset_ids.set(tmp_reward_asset_ids.encode()),
            If(new_reward_asset_id != Int(0)).Then(
                MakeInnerAssetTransferTxn(
                    asset_receiver=Global.current_application_address(),
                    xfer_asset=new_reward_asset_id,
                    fee=Int(0),
                ),
            ),
            # make sure that we haven't used reward Algos for the opt-in
            self.assert_algo_balance_is_sufficient(),
        )

    @external(authorize=Authorize.only(admin))
    def deposit_rewards(
        self, reward_ids: abi.DynamicArray[abi.Uint64], duration: abi.Uint64
    ) -> Expr:
        i = ScratchVar(type=TealType.uint64)
        deposit_id = Txn.group_index() - reward_ids.length() + i.load()
        deposit_amount = get_deposited_amount(deposit_id)
        reward_id = abi.Uint64()
        reward_asset_ids = Uint64Array()
        new_rewards = Uint64Array()
        expected_sender = self.admin.get()
        expected_receiver = Global.current_application_address()

        return Seq(
            self.assert_farm_updated_in_group(),
            Assert(self.next_duration.get() == Int(0)),
            Assert(reward_ids.length()),
            Assert(duration.get()),
            reward_asset_ids.decode(self.reward_asset_ids.get()),
            For(i.store(Int(0)), i.load() < reward_ids.length(), i.store(i.load() + Int(1))).Do(
                reward_ids[i.load()].store_into(reward_id),
                validate_transfer(
                    deposit_id,
                    reward_asset_ids[reward_id.get()],
                    expected_receiver,
                    expected_sender,
                ),
            ),
            new_rewards.decode(DEFAULT_ZERO_ARRAY),
            For(i.store(Int(0)), i.load() < reward_ids.length(), i.store(i.load() + Int(1))).Do(
                reward_ids[i.load()].store_into(reward_id),
                new_rewards.set(reward_id.get(), deposit_amount),
            ),
            If(self.duration.get() == Int(0))
            .Then(self.pending_rewards.set(new_rewards.encode()), self.duration.set(duration.get()))
            .Else(
                self.next_rewards.set(new_rewards.encode()), self.next_duration.set(duration.get())
            ),
        )

    @internal
    def move_to_next_rewards(self) -> Expr:
        return Seq(
            self.pending_rewards.set(self.next_rewards.get()),
            self.duration.set(self.next_duration.get()),
            self.next_rewards.set(DEFAULT_ZERO_ARRAY),
            self.next_duration.set(Int(0)),
        )

    @external
    def update_global_state(self) -> Expr:
        """
        Updates global state of the farm. If either total staked or duration is 0,
        so there is nobody to distribute rewards to or there are no rewards,
        then only updated_at is updated not to distribute rewards.
        """

        def extract_time_diffs(current_dt: abi.Uint64, next_dt: abi.Uint64) -> Expr:
            return Seq(
                (time_elapsed := AutoLoadScratchVar(TealType.uint64)).store(
                    Global.latest_timestamp() - self.updated_at.get()
                ),
                current_dt.set(Min(self.duration.get(), time_elapsed.load())),
                next_dt.set(Min(self.next_duration.get(), time_elapsed.load() - current_dt.get())),
            )

        current_dt = abi.Uint64()
        next_dt = abi.Uint64()
        return Seq(
            If(And(self.total_staked.get() != Int(0), self.duration.get() != Int(0))).Then(
                extract_time_diffs(current_dt, next_dt),
                # update current round rewards
                self.update_global_state_current(current_dt),
                If(self.duration.get() == Int(0)).Then(
                    self.move_to_next_rewards(),
                    If(next_dt.get()).Then(
                        self.update_global_state_current(next_dt),
                    ),
                ),
            ),
            self.updated_at.set(Global.latest_timestamp()),
        )

    def assert_farm_updated_in_group(self):
        return Assert(self.updated_at.get() == Global.latest_timestamp())

    @internal
    def update_global_state_current(self, dt: abi.Uint64) -> Expr:
        """Single run update of the global state using only the rewards in the current period

        Args:
            dt: time elapsed in the current period, must be in range [0, duration]
        """
        duration = AutoLoadScratchVar(TealType.uint64)
        total_staked = AutoLoadScratchVar(TealType.uint64)
        rewards_num = AutoLoadScratchVar(TealType.uint64)
        rpt_array = Uint64Array()
        rpt_frac_array = Uint64Array()
        pending_rewards_array = Uint64Array()
        total_rewards_array = Uint64Array()

        return Seq(
            rewards_num.store(array_length(self.reward_asset_ids.get())),
            duration.store(self.duration.get()),
            total_staked.store(self.total_staked.get()),
            rpt_array.decode(self.rpt.get()),
            rpt_frac_array.decode(self.rpt_frac.get()),
            pending_rewards_array.decode(self.pending_rewards.get()),
            total_rewards_array.decode(self.total_rewards.get()),
            For((i := AutoLoadScratchVar()).store(Int(0)), i < rewards_num, i.increment()).Do(
                Seq(
                    # calculate the reward per token for this asset
                    (pending_reward := AutoLoadScratchVar(TealType.uint64)).store(
                        pending_rewards_array[i]
                    ),
                    (rpt_calc := RPTCalculator()).run(
                        dt.get(),
                        duration.load(),
                        rpt_array[i],
                        rpt_frac_array[i],
                        pending_reward.load(),
                        total_staked.load(),
                    ),
                    rpt_array.set(i, rpt_calc.result.rpt.load()),
                    rpt_frac_array.set(i, rpt_calc.result.rpt_frac.load()),
                    pending_rewards_array.set(
                        i, pending_reward - rpt_calc.result.distributed_reward
                    ),
                    total_rewards_array.set(
                        i, total_rewards_array[i] + rpt_calc.result.distributed_reward
                    ),
                )
            ),
            self.duration.decrement(dt.get()),
            self.rpt.set(rpt_array.encode()),
            self.rpt_frac.set(rpt_frac_array.encode()),
            self.pending_rewards.set(pending_rewards_array.encode()),
            self.total_rewards.set(total_rewards_array.encode()),
        )

    def update_local_state(self, escrow_creator: Expr, escrow_address: Expr) -> Expr:
        rpt_array = Uint64Array()
        rpt_frac_array = Uint64Array()
        user_staked = AutoLoadScratchVar(TealType.uint64)
        user_rpt_array = Uint64Array()
        user_rpt_frac_array = Uint64Array()
        user_accrued_rewards_array = Uint64Array()
        rewards_num = array_length(self.reward_asset_ids.get())

        return Seq(
            rpt_array.decode(self.rpt.get()),
            rpt_frac_array.decode(self.rpt_frac.get()),
            # local state loads from Escrow's creator
            user_staked.store(self.user_staked[escrow_creator].get()),
            user_rpt_array.decode(self.user_rpt[escrow_creator].get()),
            user_rpt_frac_array.decode(self.user_rpt_frac[escrow_creator].get()),
            user_accrued_rewards_array.decode(self.user_accrued_rewards[escrow_creator].get()),
            For((i := AutoLoadScratchVar()).store(Int(0)), i < rewards_num, i.increment()).Do(
                Seq(
                    (rpt := AutoLoadScratchVar(TealType.uint64)).store(rpt_array[i]),
                    (rpt_frac := AutoLoadScratchVar(TealType.uint64)).store(rpt_frac_array[i]),
                    # user RPT calculations
                    # the stake used for calculations is the one that we have stored
                    # in the local state (0 at the first update)
                    (user_rpt := AutoLoadScratchVar(TealType.uint64)).store(user_rpt_array[i]),
                    (user_rpt_frac := AutoLoadScratchVar(TealType.uint64)).store(
                        user_rpt_frac_array[i]
                    ),
                    (user_calc := UserRewardCalculator()).run(
                        rpt.load(),
                        rpt_frac.load(),
                        user_rpt.load(),
                        user_rpt_frac.load(),
                        self.user_staked[escrow_creator].get(),
                    ),
                    user_accrued_rewards_array.set(
                        i, user_accrued_rewards_array[i] + user_calc.result.accrued_reward
                    ),
                )
            ),
            escrow_auth := AccountParam.authAddr(escrow_address),
            (escrow_balance := AssetHolding.balance(escrow_address, self.staked_asset_id.get())),
            (new_user_stake := AutoLoadScratchVar(TealType.uint64)).store(
                (escrow_auth.value() == Global.zero_address()) * escrow_balance.value()
            ),
            # global state updates that depend on local state changes
            self.total_staked.set(
                self.total_staked.get() - user_staked.load() + new_user_stake.load()
            ),
            self.update_number_of_stakers(user_staked.load(), new_user_stake.load()),
            # save local state changes
            self.user_staked[escrow_creator].set(new_user_stake.load()),
            self.user_rpt[escrow_creator].set(rpt_array.encode()),  # uses global array
            self.user_rpt_frac[escrow_creator].set(rpt_frac_array.encode()),  # uses global array
            self.user_accrued_rewards[escrow_creator].set(user_accrued_rewards_array.encode()),
        )

    @external
    def update_state(
        self,
        escrow: abi.Application,
        escrow_account: abi.Account,
        user: abi.Account,
        staked_asset: abi.Asset,  # don't use (not validated)
    ):
        escrow_creator = escrow.params().creator_address()
        escrow_address = escrow.params().address()

        return Seq(
            escrow_creator,
            escrow_address,
            Assert(escrow_creator.hasValue()),
            Assert(escrow_address.hasValue()),
            Assert(
                escrow_address.value() == escrow_account.address(),
                comment="balance and auth address must taken from app's account",
            ),
            Assert(
                self.user_staked[escrow_creator.value()].exists(),
                comment="creator is not opted in",
            ),
            Assert(self.validate_escrow(escrow, user), comment="Escrow validation failed"),
            self.update_global_state(),
            self.update_local_state(escrow_creator.value(), escrow_address.value()),
        )

    def update_number_of_stakers(self, user_previous_stake: Expr, user_current_stake: Expr):
        @Subroutine(TealType.none)
        def _update_number_of_stakers(user_previous_stake: Expr, user_current_stake: Expr):
            return (
                If(And(user_previous_stake, Not(user_current_stake)))
                .Then(self.number_of_stakers.decrement())
                .ElseIf(And(Not(user_previous_stake), user_current_stake))
                .Then(self.number_of_stakers.increment())
            )

        return _update_number_of_stakers(user_previous_stake, user_current_stake)

    @external
    def claim_rewards(self, account: abi.Account, reward_ids: abi.DynamicArray[abi.Uint64]) -> Expr:
        """Claim rewards for a given asset

        Args:
            account: The account to claim rewards for
            reward_ids: Indices into the rewards array

        Example:
            asset_ids = [1, 0, 2]
            will claim rewards at indices [1, 0 , 2] whichcever assets that will be

        Txn.assets:
            All assets that are referenced by reward_asset_ids[asset_ids[x]]
            x ∈ [0, n] where n is the number of awailable rewards
        """
        rewards_num = ScratchVar(TealType.uint64)  # the amount of reward assets
        reward_asset_ids = Uint64Array()
        claimed_rewards = Uint64Array()
        user_accrued_rewards = Uint64Array()
        user_claimed_rewards = Uint64Array()

        requests_num = ScratchVar(TealType.uint64)
        index_into_rewards = abi.Uint64()
        reward_amount = ScratchVar(TealType.uint64)
        reward_asset_id = ScratchVar(TealType.uint64)  # the Algorand Asset ID of the reward asset
        return Seq(
            # load arrays
            reward_asset_ids.decode(self.reward_asset_ids.get()),
            rewards_num.store(reward_asset_ids.length()),
            claimed_rewards.decode(self.claimed_rewards.get()),
            user_accrued_rewards.decode(self.user_accrued_rewards[account.address()].get()),
            user_claimed_rewards.decode(self.user_claimed_rewards[account.address()].get()),
            requests_num.store(reward_ids.length()),
            # process rewards
            For(
                (i := ScratchVar()).store(Int(0)),
                i.load() < requests_num.load(),
                i.store(i.load() + Int(1)),
            ).Do(
                Seq(
                    reward_ids[i.load()].store_into(index_into_rewards),
                    Assert(
                        index_into_rewards.get() <= rewards_num.load(),
                        comment="Requested reward index out of bounds",
                    ),
                    reward_asset_id.store(reward_asset_ids[index_into_rewards.get()]),
                    reward_amount.store(user_accrued_rewards[index_into_rewards.get()]),
                    SendToAddress(account.address(), reward_asset_id.load(), reward_amount.load()),
                    # Clear user's reward in that asset
                    # This also prevents repeated withdrawal of the same asset
                    user_accrued_rewards.set(index_into_rewards.get(), Int(0)),
                    # Update global & local stats tracker
                    claimed_rewards.set(
                        index_into_rewards.get(),
                        claimed_rewards[index_into_rewards.get()] + reward_amount.load(),
                    ),
                    user_claimed_rewards.set(
                        index_into_rewards.get(),
                        user_claimed_rewards[index_into_rewards.get()] + reward_amount.load(),
                    ),
                )
            ),
            # store
            self.claimed_rewards.set(claimed_rewards.encode()),
            self.user_accrued_rewards[account.address()].set(user_accrued_rewards.encode()),
            self.user_claimed_rewards[account.address()].set(user_claimed_rewards.encode()),
        )

    @internal
    def exit_farm(self):
        total_rewards = Uint64Array()
        user_accrued_rewards = Uint64Array()
        return Seq(
            (user_staked := AutoLoadScratchVar(TealType.uint64)).store(
                self.user_staked[Txn.sender()].get()
            ),
            self.total_staked.set(self.total_staked.get() - user_staked),
            self.update_number_of_stakers(user_staked.load(), Int(0)),
            total_rewards.decode(self.total_rewards.get()),
            user_accrued_rewards.decode(self.user_accrued_rewards[Txn.sender()].get()),
            For(
                (i := AutoLoadScratchVar()).store(Int(0)),
                i < MAX_FARMED_ASSETS,
                i.store(i + Int(1)),
            ).Do(
                total_rewards.set(i, total_rewards[i] - user_accrued_rewards[i]),
            ),
            self.total_rewards.set(total_rewards.encode()),
        )
