"""Subcommand implementations; each module they need is imported on use."""


def dispatch(command: str, args: list[str]) -> int:
    handler = globals().get('cmd_' + command.replace('-', '_'))
    if handler is None:
        print(f'natemu-test {command}: not implemented yet')
        return 2
    return handler(args)
