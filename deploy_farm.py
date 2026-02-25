import base64

import algosdk

import pactsdk
from pactsdk.utils import get_selector, sp_fee

import farm

ESCROW_LEN = 346
BOX_COST = 2500 + 400 * (ESCROW_LEN + len("Escrow"))
CREATE_FARM_SIG = get_selector("create(application,asset,account,account)void")


def deploy_farm_contract(sender: str, signer_pk: str, staked_asset_id: int) -> int:
    group = get_deploy_farm_tx_group(sender, staked_asset_id)
    sign_and_send(group, signer_pk)
    tx_info = wait_for_confirmation(group.transactions[-1].get_txid())
    return tx_info["application-index"]


def get_deploy_farm_tx_group(sender: str, staked_asset_id: int):
    farm = Farm()

    farm.compile(algod)

    compiled_SSC = algod.compile(farm.approval_program)
    compiled_clear = algod.compile(farm.clear_program)

    ssc_raw: str = compiled_SSC["result"]
    clear_raw: str = compiled_clear["result"]

    suggested_params = algod.suggested_params()

    gas_station = pactsdk.get_gas_station()

    fund_tx = algosdk.transaction.PaymentTxn(
        sender=sender,
        receiver=gas_station.app_address,
        amt=100_000 + BOX_COST,
        sp=suggested_params,
    )

    app_args = [
        CREATE_FARM_SIG,
        algosdk.abi.UintType(8).encode(1),
        0,
        0,
        algosdk.abi.UintType(8).encode(1),
    ]
    create_tx = algosdk.transaction.ApplicationCreateTxn(
        sender=sender,
        on_complete=algosdk.transaction.OnComplete.NoOpOC,
        approval_program=base64.b64decode(ssc_raw),
        clear_program=base64.b64decode(clear_raw),
        global_schema=algosdk.transaction.StateSchema(7, 10),
        local_schema=algosdk.transaction.StateSchema(2, 4),
        app_args=app_args,
        accounts=[settings.ALGORAND_MULTISIG_ADMIN_ADDRESS],
        foreign_assets=[staked_asset_id],
        foreign_apps=[gas_station.app_id],
        boxes=[(0, "Escrow")],
        sp=sp_fee(suggested_params, 3000),
        extra_pages=2,
    )

    return pactsdk.TransactionGroup([fund_tx, create_tx])
