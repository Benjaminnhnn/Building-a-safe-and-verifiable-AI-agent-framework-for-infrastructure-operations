"""Demonstrate the pinned AIHS prototype's rejected-action fallback.

This uses stub classifiers and does not train, unpickle, or execute actions.
"""

import argparse
import json
import sys
from pathlib import Path


class StubClassifier:
    def __init__(self, prediction):
        self.prediction = prediction
        self.calls = 0

    def predict(self, _features):
        self.calls += 1
        return [self.prediction]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("prototype_dir", type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(args.prototype_dir.resolve()))
    from AIHS_Training import Controller

    controller = Controller.__new__(Controller)
    controller.actionlist = ["NOP", "MSR", "ER", "RB", "RI", "RMA"]
    controller.classifiers = {
        action: StubClassifier(1 if action == "NOP" else 0)
        for action in controller.actionlist
    }
    observed = controller._control_process([0] * 10, "RB")
    print(json.dumps({
        "proposed_action": "RB",
        "proposed_classifier_result": 0,
        "cheapest_action": "NOP",
        "cheapest_classifier_result": 1,
        "observed_upstream_output": observed,
        "nop_classifier_calls": controller.classifiers["NOP"].calls,
        "expected_if_paper_cost_order_worked": "NOP",
    }, sort_keys=True))
    assert observed == "call human"
    assert controller.classifiers["NOP"].calls == 0


if __name__ == "__main__":
    main()
