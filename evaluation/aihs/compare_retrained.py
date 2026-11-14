"""Compare two trusted AIHS prototype training runs on the bundled CSV.

Only pass local copies of the pinned upstream source: pickle can execute code.
This measures in-sample agreement, not recovery accuracy or generalization.
"""

import argparse
import json
import pickle
import sys
from collections import Counter
from pathlib import Path

import pandas as pd


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("first", type=Path, help="first AIHS_prototype working copy")
    parser.add_argument("second", type=Path, help="second AIHS_prototype working copy")
    args = parser.parse_args()

    # Training as a script serializes classes under __main__, so the imports
    # must be present in this module before unpickling the generated models.
    sys.path.insert(0, str(args.first.resolve()))
    from AIHS_Training import Mapper, Predictor, Controller  # noqa: F401
    globals().update(Mapper=Mapper, Predictor=Predictor, Controller=Controller)

    rows = pd.read_csv(args.first / "dataset/train.csv")
    outputs = {}
    for name, workdir in (("first", args.first), ("second", args.second)):
        models = {}
        for component in ("mapper", "predictor", "controller"):
            with (workdir / "model" / f"{component}.pkl").open("rb") as handle:
                models[component] = pickle.load(handle)
        features = models["mapper"].Map(rows.attributes.tolist())
        proposed = models["predictor"].predict(features).tolist()
        final = models["controller"].control(features, proposed)
        outputs[name] = {"proposed": proposed, "final": final}

    first, second = outputs["first"], outputs["second"]
    print(json.dumps({
        "rows": len(rows),
        "proposed_agreement": sum(a == b for a, b in zip(first["proposed"], second["proposed"])),
        "final_agreement": sum(a == b for a, b in zip(first["final"], second["final"])),
        "first_final_counts": dict(Counter(first["final"])),
        "second_final_counts": dict(Counter(second["final"])),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
