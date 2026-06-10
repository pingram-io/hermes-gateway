"""Pingram Voice alpha notice wizard."""

from pingram_gateway.core.constants import VOICE_ALPHA_MESSAGE


def setup_voice() -> None:
    from hermes_cli.setup import color, Colors, print_header, print_info

    print_header("Pingram Voice")
    print()
    print_info(VOICE_ALPHA_MESSAGE)
    print()
    print(color("  📧 hello@pingram.io", Colors.BOLD, Colors.CYAN))
