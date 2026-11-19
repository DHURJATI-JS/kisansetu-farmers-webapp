"""Importing this package registers every action handler."""
from app.services.actions import account, admin, farm, hiring, money, schemes, shop  # noqa: F401
from app.services.actions.base import REGISTRY, ai_action_specs, cancel_action, go_action, prepare_action, vote_proposal  # noqa: F401
