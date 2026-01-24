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
            
            # Development tools
            pythonPackages.black
            pythonPackages.pylint
            pythonPackages.mypy
            pythonPackages.isort
            
            # Protocol buffers compiler
            pkgs.protobuf
          ];

          shellHook = ''
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
            echo ""
            echo "To regenerate proto files:"
            echo "  cd proto && python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. *.proto"
            echo ""
          '';
        };
      }
    );
}
