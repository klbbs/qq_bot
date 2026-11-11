"""Install QQ response style and delivery after all bridge plugins are loaded."""

MIN_REACTIVE_COOLDOWN_SEC = 15


def install():
    import qq_concise_policy
    import qq_compact_delivery
    from nonebot_plugin_hermes.config import plugin_config

    qq_concise_policy.install()
    qq_compact_delivery.install()
    # This configuration belongs to the QQ bridge process. The native handlers
    # already bypass this gate for explicit @bot triggers and direct commands.
    plugin_config.hermes_reactive_post_reply_cooldown_sec = max(
        MIN_REACTIVE_COOLDOWN_SEC,
        plugin_config.hermes_reactive_post_reply_cooldown_sec,
    )
