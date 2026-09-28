Pramana Studio for Mac
======================

1. Drag Pramana into Applications.
2. Open it. The first time, macOS may say it cannot verify the developer (the app is not
   notarized): open System Settings > Privacy & Security, scroll down, click "Open Anyway".
   (Or right-click Pramana > Open.)
3. Paste your API key when Pramana asks (DeepSeek, OpenAI, Anthropic, NVIDIA, OpenRouter, ...;
   the provider is detected from the key). Optionally set a model. Click "Use this model".
   The key stays on this Mac only: ~/Library/Application Support/Pramana/settings.json,
   readable only by you. "Remove saved key" in the Model panel deletes it.
4. Paste a GitHub issue link (or describe the bug and give the repo) and press Solve.

Needs git and python3: macOS offers to install the Command Line Tools the first time they are
used (or run: xcode-select --install). Pull requests use the GitHub CLI (gh) if installed.
Runs and cloned repositories are kept in ~/Library/Application Support/Pramana.
