"""Who may see what a project cost (§10; O14).

O14 asks for something a permission alone cannot express: a manager sees cost
**on their own projects**, and the owner sees it on all of them. So there is one
function here, used by every serializer that carries a money figure, rather than
the same three lines of reasoning copied into each of them.
"""

from __future__ import annotations

from accounts.permissions_registry import PERM


def may_see_project_cost(request, project) -> bool:
    """Whether this viewer may see what ``project`` has spent (O14).

    The rule:

    * ``project.view_margin`` — owner and admin — sees cost on every project,
      since they can see the contract value behind it anyway;
    * ``project.view_cost`` sees cost only on projects they manage;
    * everybody else sees none.

    With **no request in context** nothing is withheld. An export, a management
    command or a report rendering off-request has already passed whatever check
    applies to it, and silently emptying its output would be a bug that only
    surfaces in a figure somebody trusted.
    """
    if request is None:
        return True

    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return False

    from accounts.services import resolve_permissions

    permissions = resolve_permissions(user)
    if permissions.has(PERM.PROJECT_VIEW_MARGIN):
        return True
    if not permissions.has(PERM.PROJECT_VIEW_COST):
        return False
    return getattr(project, "manager_id", None) == user.pk


def may_see_project_margin(request) -> bool:
    """Whether this viewer may see contract value and margin (O14).

    Not scoped per project: a manager is given cost precisely so they can work
    to a budget without being shown what the work sells for.
    """
    if request is None:
        return True

    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return False

    from accounts.services import resolve_permissions

    return resolve_permissions(user).has(PERM.PROJECT_VIEW_MARGIN)
