"""T168 (sprint2-backend) — `update_subscription_quantity`.

Internal helper that calls Stripe's subscription-modify API to set
the seat count on a multi-seat subscription. Used by T165's accept-
invite and T166's remove-member endpoints to keep Stripe billing in
sync with the org's actual seat count.

Pinned behaviors:
  - retrieves the subscription to find the (single) line item ID
  - calls modify with items=[{id: <item_id>, quantity: <count>}]
  - proration_behavior="create_prorations" (mid-cycle adds charge a
    partial amount, removes credit a partial refund — making the
    proration explicit)
"""

from __future__ import annotations

from unittest.mock import patch

from infra.stripe import update_subscription_quantity


def test_update_subscription_quantity_calls_modify_with_quantity() -> None:
    """The helper round-trips: retrieve sub → grab first item id →
    modify with that item id and the new quantity. Pin: the call
    pattern is the API contract with Stripe."""
    fake_sub = {"items": {"data": [{"id": "si_seat_item"}]}}
    with patch(
        "stripe.Subscription.retrieve", return_value=fake_sub
    ) as mock_retrieve, patch(
        "stripe.Subscription.modify"
    ) as mock_modify:
        update_subscription_quantity("sub_test_xyz", 5)

    mock_retrieve.assert_called_once_with("sub_test_xyz")
    mock_modify.assert_called_once_with(
        "sub_test_xyz",
        items=[{"id": "si_seat_item", "quantity": 5}],
        proration_behavior="create_prorations",
    )


def test_update_subscription_quantity_proration_is_explicit() -> None:
    """Mutation: if proration_behavior is dropped, mid-cycle
    seat changes might not prorate (Stripe's default depends on
    account settings — explicit > implicit). Pin: the flag is
    load-bearing, not decoration."""
    fake_sub = {"items": {"data": [{"id": "si_x"}]}}
    with patch(
        "stripe.Subscription.retrieve", return_value=fake_sub
    ), patch("stripe.Subscription.modify") as mock_modify:
        update_subscription_quantity("sub_y", 3)

    kwargs = mock_modify.call_args.kwargs
    assert kwargs["proration_behavior"] == "create_prorations"
