# Contributing to EufyLife API Integration

Contributions to this project are welcome! Here are some guidelines to help you get started.

## Setting up your development environment

The easiest way to get started is to use the Dev Container feature of Visual Studio Code. This method will create a fully configured development environment with all the dependencies.

### Prerequisites

- [git](https://git-scm.com/)
- [Docker](https://www.docker.com/) or [Podman](https://podman.io/)
- [Visual Studio Code](https://code.visualstudio.com/)
- [Remote - Containers](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers) extension for VS Code

### Setup

1. Fork this repository
2. Clone your fork
3. Open the repository in Visual Studio Code
4. When prompted, reopen in container
5. Wait for the container to build and start
6. Run `container install` to install the integration in the development environment

## Development workflow

### Testing your changes

1. Make your changes to the integration code
2. Run `container install` to install your changes
3. Restart Home Assistant to see your changes: `container restart`

### Code quality

We use several tools to ensure code quality:

- [Ruff](https://github.com/astral-sh/ruff) for linting and formatting
- Type hints are encouraged

Run these tools before submitting your PR:

```bash
# Format code
python -m ruff format .

# Lint code
python -m ruff check .
```

### Testing

If you want to add tests (which is highly encouraged), place them in the `tests` directory. Use pytest-homeassistant-custom-component to help with testing custom components.

## Pull Request Guidelines

1. **Create a new branch** for your feature or bug fix
2. **Make your changes** with clear, descriptive commit messages
3. **Test your changes** thoroughly
4. **Update documentation** if needed
5. **Submit a pull request** with a clear description of your changes

### PR Title Format

Use conventional commit format for PR titles:

- `feat: add new feature`
- `fix: resolve bug`
- `docs: update documentation`
- `style: formatting changes`
- `refactor: code refactoring`
- `test: add tests`

## Bug Reports and Feature Requests

Please use the GitHub issue tracker to:

- Report bugs
- Request new features
- Ask questions

When reporting bugs, include:

- Home Assistant version
- Integration version
- Steps to reproduce
- Expected vs actual behavior
- Relevant logs

## Code Guidelines

### Python Style

- Follow PEP 8
- Use type hints
- Add docstrings to public methods
- Keep functions small and focused

### Home Assistant Integration Guidelines

- Follow [Home Assistant development guidelines](https://developers.home-assistant.io/)
- Use the data coordinator pattern for API calls
- Implement proper error handling
- Add appropriate logging
- Use Home Assistant's built-in features (device registry, entity registry, etc.)

## API Development

When working with the EufyLife API:

- Be respectful of rate limits
- Handle API errors gracefully
- Don't hardcode credentials in tests
- Document any new API endpoints discovered

## Publishing a fork to HACS

Users install this integration through [HACS](https://hacs.xyz/). HACS copies only
`custom_components/eufylife_api/` into Home Assistant, so the layout below must stay intact:

- `hacs.json` - HACS metadata (`content_in_root` must stay `false`)
- `custom_components/eufylife_api/manifest.json` - `version` must match `version.txt`
- `.github/workflows/validate.yml` - runs the `hacs/action` validation on every push
- `.github/workflows/release.yml` - creates the `vX.Y.Z` release whenever `version.txt` changes

Checklist for a fork:

1. Push the repository to GitHub (it must be public)
2. Give the repository **topics**. The `repository` check of the HACS validation step in
   `.github/workflows/validate.yml` fails on a repository without any ("The repository has no
   valid topics"), and `hacs` and `integration` are the two HACS uses for an integration
   (`home-assistant` is a safe third). They are set behind the gear icon of the repository
   page's *About* panel
3. Bump `version.txt` **and** the `version` key in `manifest.json` to the same value and merge
   to the default branch; the Release workflow creates the matching tag. HACS only offers
   versions that have a release - a repository without releases cannot be installed
4. Never commit app dumps or local credential files; `.gitignore` already excludes
   `*.apk`, `*.apkm`, `eufy_creds.json` and `.env`. GitHub rejects files larger than 100 MB
5. In Home Assistant open HACS -> Integrations -> three-dot menu -> **Custom repositories**,
   paste the repository URL, choose category **Integration** and click Add
6. Search for "EufyLife API", download it and restart Home Assistant
7. Add the integration in **Settings -> Devices & Services -> Add Integration -> EufyLife API**
   and enter the EufyLife email, password and country there. The credentials are stored in the
   Home Assistant config entry, never in the repository, and the config flow uses them to
   obtain and refresh the API tokens

Being in the HACS default list (so users do not have to add a custom repository) is optional
and requires brand images in this repository or a pull request against
[home-assistant/brands](https://github.com/home-assistant/brands), plus a PR to the
[hacs/default](https://github.com/hacs/default) list.

## Questions?

If you have questions about contributing, feel free to:

- Open a discussion on GitHub
- Create an issue for clarification
- Reach out to the maintainers

Thank you for your contributions! 🎉 