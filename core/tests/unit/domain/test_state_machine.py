from dataclasses import dataclass

import pytest

from tightrein.domain.enums import LabeledEnum
from tightrein.domain.state_machine import InvalidTransition, Rule, SideEffect, apply, conditions_hold, resolve


class Light(LabeledEnum):
    RED = ("red", "红")
    GREEN = ("green", "绿")


class Press(LabeledEnum):
    GO = ("go", "通行")
    STOP = ("stop", "停止")


class Effect(LabeledEnum):
    BEEP = ("beep", "提示音")


@dataclass(frozen=True)
class Ctx:
    urgent: bool = False
    note: str | None = None


RULES = (
    Rule(Press.GO, frozenset({Light.RED}), Light.GREEN, when=(("urgent", True),), effects=(Effect.BEEP,),
         payload=(("level", 2),), carry=("note",)),
    Rule(Press.GO, frozenset({Light.RED}), Light.GREEN),
    Rule(Press.STOP, frozenset({Light.RED, Light.GREEN}), None),
)


def test_first_matching_rule_wins():
    assert resolve(RULES, Light.RED, Press.GO, Ctx(urgent=True)) is RULES[0]
    assert resolve(RULES, Light.RED, Press.GO, Ctx()) is RULES[1]


def test_target_none_keeps_state():
    assert apply(RULES, Light.GREEN, Press.STOP, Ctx()) == (Light.GREEN, ())


def test_effects_carry_payload_and_context_fields():
    state, effects = apply(RULES, Light.RED, Press.GO, Ctx(urgent=True, note="夜间"))
    assert state is Light.GREEN
    assert effects == (SideEffect(Effect.BEEP, {"level": 2, "note": "夜间"}),)


def test_missing_carry_field_is_an_error():
    with pytest.raises(ValueError, match="note"):
        apply(RULES, Light.RED, Press.GO, Ctx(urgent=True))


def test_unknown_combination_raises_invalid_transition():
    with pytest.raises(InvalidTransition) as error:
        apply(RULES, Light.GREEN, Press.GO, Ctx())
    assert error.value.state is Light.GREEN
    assert error.value.event is Press.GO
    assert "green" in str(error.value)


def test_conditions_hold():
    assert conditions_hold((), Ctx())
    assert conditions_hold((("urgent", False),), Ctx())
    assert not conditions_hold((("urgent", True),), Ctx())
