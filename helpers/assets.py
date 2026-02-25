"""Abstraction layer for Algos and ASAs operations"""

from pyteal import AssetHolding, Balance, Expr, Global, If, MinBalance, Seq, Subroutine, TealType


@Subroutine(TealType.uint64)
def get_currrent_app_balance(asset_id: Expr) -> Expr:
    """Get the current balance of the currently executing application

    For assets simply return the asset holding
    For algos (asset_id=0) return the balance minus min. balance
    """
    asset_balance = AssetHolding.balance(Global.current_application_address(), asset_id)

    return If(
        asset_id,
        Seq(asset_balance, asset_balance.value()),
        Balance(Global.current_application_address())
        - MinBalance(Global.current_application_address()),
    )
