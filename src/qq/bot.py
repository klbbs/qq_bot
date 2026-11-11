import nonebot
from nonebot.adapters.onebot.v11 import Adapter

nonebot.init()
nonebot.get_driver().register_adapter(Adapter)
nonebot.load_plugin("nonebot_plugin_hermes")

import outbound
outbound.register()

import qq_delivery_guard
qq_delivery_guard.install()

import qq_features
qq_features.register()

import qq_control
qq_control.install_client_adapter()

import qq_session_recovery
qq_session_recovery.install()

import qq_window_bridge
qq_window_bridge.install()

import qq_share_parse
qq_share_parse.install()
nonebot.load_plugin("practice_commands")
nonebot.load_plugin("control_commands")

import qq_compact_bootstrap
qq_compact_bootstrap.install()

import qq_share_cards
qq_share_cards.install()
import qq_card_api
qq_card_api.register()
import qq_friend_auto  # 自动同意好友申请


if __name__ == "__main__":
    nonebot.run()
