{
  description = "Volvo On Call CN Home Assistant Integration Development Environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        python = pkgs.python313;
        pythonPackages = python.pkgs;
      in
      {
        devShells.default = pkgs.mkShell {
          buildInputs = [
            python
            pythonPackages.pip
            pythonPackages.setuptools
            pythonPackages.wheel

            # Core dependencies from manifest.json
            pythonPackages.grpcio
            pythonPackages.grpcio-tools

            # Testing dependencies
            pythonPackages.pytest
            pythonPackages.pytest-asyncio
            pythonPackages.pytest-cov
            pythonPackages.pytest-timeout
            
            # Development tools
            pythonPackages.black
            pythonPackages.pylint
            pythonPackages.mypy
            pythonPackages.isort

            # Protocol buffers compiler
            pkgs.protobuf
          ];

          shellHook = ''
            # Setup local pip directory
            export LOCALDIR="$PWD/.local"
            export PYTHONPATH="$LOCALDIR/lib/python3.13/site-packages:$PYTHONPATH"
            export PATH="$LOCALDIR/bin:$PATH"
            
            # Unset PIP environment variables that might conflict
            unset PIP_PREFIX
            unset PYTHONUSERBASE
            
            # Create directory structure
            mkdir -p "$LOCALDIR/lib/python3.13/site-packages"
            
            # Install pytest-homeassistant-custom-component if not already installed
            if ! python -c "import pytest_homeassistant_custom_component" 2>/dev/null; then
              echo "Installing pytest-homeassistant-custom-component..."
              pip install --target="$LOCALDIR/lib/python3.13/site-packages" pytest-homeassistant-custom-component
            fi
            
            echo "🚗 Volvo On Call CN Development Environment"
            echo "Python version: $(python --version)"
            echo ""
            echo "Available commands:"
            echo "  - python: Python 3.13 interpreter"
            echo "  - pip: Package installer"
            echo "  - protoc: Protocol buffer compiler"
            echo "  - black: Code formatter"
            echo "  - pylint: Linter"
            echo "  - mypy: Type checker"
            echo "  - pytest: Test runner"
            echo ""
            echo "Testing:"
            echo "  - pytest: Run all tests"
            echo "  - pytest --cov: Run tests with coverage report"
            echo "  - pytest tests/test_config_flow.py: Run specific test file"
            echo ""
            echo "To regenerate proto files:"
            echo "  cd proto && python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. *.proto"
            echo ""
          '';
        };
      }
    );
}
