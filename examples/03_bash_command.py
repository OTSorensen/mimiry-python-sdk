"""``mimiry.run()`` — run a raw bash command on a GPU.

Handy for one-off GPU jobs like ffmpeg with NVENC.

Run:
    export MIMIRY_SSH_KEY=~/.ssh/mimiry   # your registered SSH key
    python examples/03_bash_command.py
"""

import mimiry


if __name__ == "__main__":
    print("Submitting bash command to Mimiry — expect ~2 min cold start...")
    result = mimiry.run(
        # Defaults to an A100; run `mimiry availability` to choose a GPU/provider.
        image="nvcr.io/nvidia/pytorch:24.01-py3",
        command="nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv",
        timeout=900,
    )
    print(f"\nSession: {result.session_id}")
    print(f"State:   {result.state}")
    print(f"Exit:    {result.exit_code}")
    print("\n── Logs ────────────────────────────────────────────────────────────")
    print(result.logs)
