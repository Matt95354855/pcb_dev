"""Explicit host configuration for the prompt-first studio; no implicit requests."""

from __future__ import annotations

import os

from .agent import Budget
from .providers import ChatCompletionsProvider, ModelProfile, ProviderConfig
from .transactions import read_json


def configured_manager(args):
    """Construct providers without inference; credentials remain in the environment."""
    from .project import DemoManagerProvider, ProjectManager

    if args.demo:
        if args.claude_config or args.model_key_env:
            raise ValueError("The scripted demo cannot use live model credentials or Claude")
        provider = DemoManagerProvider()
    else:
        config = ProviderConfig.from_dict(read_json(args.model_config)) if args.model_config else ProviderConfig(
            profile=ModelProfile(model=args.model, context_window=args.context_window,
                                 max_output_tokens=args.max_output_tokens, token_parameter="max_tokens"),
            endpoint=args.endpoint, api_key_env=args.model_key_env,
            offline=not args.allow_network, allow_network=args.allow_network,
            max_requests=512, max_total_tokens=16_000_000,
        )
        if ("REPLACE" in config.profile.model.upper() or "REPLACE" in config.endpoint.upper()
                or "POD_ID" in config.endpoint.upper()):
            raise ValueError("Replace the template endpoint and served model ID before launching the studio")
        provider = ChatCompletionsProvider(config)
        if provider.network_enabled and not args.allow_network:
            raise ValueError("A remote Qwen endpoint requires --allow-network")
    claude = None
    if args.claude_config:
        if not args.allow_network:
            raise ValueError("Optional Claude reviews require explicit --allow-network")
        from .claude import ClaudeConfig, ClaudeProvider
        from .quota import TokenLedger
        config = ClaudeConfig.from_dict(read_json(args.claude_config))
        if not config.allow_network or "REPLACE" in config.model.upper():
            raise ValueError("Configure an actual Claude model and its explicit network permission")
        claude = ClaudeProvider(config, ledger=TokenLedger(args.project))
    return ProjectManager(args.project, provider,
        budget=Budget(max_steps=args.max_steps, max_tokens=args.max_tokens),
        bindings=args.bindings, claude=claude, hierarchical=args.hierarchical, review_patches=args.review_patches)


def launch_studio(args) -> int:
    from .studio import serve_studio

    token = None
    if args.auth_env:
        token = os.environ.get(args.auth_env)
        if not token or not token.strip() or any(ord(char) < 33 for char in token):
            raise ValueError("Studio access-token environment variable is missing or invalid")
    manager = configured_manager(args)
    print(f"PCB Studio · {'scripted demo' if args.demo else 'self-hosted Qwen'} · {args.host}:{args.port}")
    print("Claude reviews require an explicit human action and the project token allowance.")
    serve_studio(manager, host=args.host, port=args.port, auth_token=token, public_origin=args.public_origin)
    return 0
