"""``mimiry.run()`` — raw bash command on a GPU. Mirrors the video-encoding user's
"just give me ffmpeg with NVENC" pattern.

Run:
    export MIMIRY_SSH_KEY=~/.ssh/mimiry_oliver_new
    python examples/03_bash_command.py
"""

import mimiry


if __name__ == "__main__":
    print("Submitting bash command to Mimiry T4 — expect ~2 min cold start...")
    result = mimiry.run(
        image="nvcr.io/nvidia/cuda:12.6.2-runtime-ubuntu24.04",
        gpu="T4",
        provider="gcp",  # T4 is GCP-only today; query /availability before assuming otherwise
        command="nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv",
        timeout=900,
    )
    print(f"\nSession: {result.session_id}")
    print(f"State:   {result.state}")
    print(f"Exit:    {result.exit_code}")
    print("\n── Logs ────────────────────────────────────────────────────────────")
    print(result.logs)
