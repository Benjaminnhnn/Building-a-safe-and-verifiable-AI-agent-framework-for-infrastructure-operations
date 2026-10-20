# Runtime verification

The deployable verification package contains schema-based probes and incident state transitions. Replay-only communication-contract models and the former independent-verifier prototype are under evaluation/agent_replay/verification.

- state_machine.py rejects direct RESOLVED transitions. It accepts a resolved transition only with a complete, eligible VerificationResult, full allowed/forbidden/related probe sets, and a valid stability window.
- probes.py contains read-only Moodle HTTP, TCP, login, and synthetic-transaction probes.
- verifier_contract.py defines probe contracts and the contract runner.
- verification_agent.py validates typed probe results and stability evidence.

The staging drill's independent verifier runs from automation/moodle-independent-verifier.py. It validates the safe-action receipt and audit chain, collects live samples for at least 120 seconds with no sampling gap above 60 seconds, then records the verifier-authorized transition. No alert handler or executor can resolve an incident.
