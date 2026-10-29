# README intro claims only what users can do

## Context

The README's opening sentence, which PyPI shows at the top of the project
page, said the SDK gives "full control over locality and providers". Provider
names are withheld from users and they cannot pick a provider, so the
sentence promised something the product does not offer.

## Goal

The opening sentence describes the SDK in terms a user can verify, without
claiming provider control.

## Acceptance criteria

1. README's opening sentence no longer mentions providers or "full control".
2. It still says what the SDK is: a Python SDK plus a CLI for running GPU
   jobs and managing sessions and volumes on Mimiry.
3. No other line changes.

## Non-goals

- A release. The change reaches PyPI with the next version.
- Rewording anything else in the README.

## Verification

Read the rendered README on GitHub; after the next release, the PyPI project
page shows the new sentence.
