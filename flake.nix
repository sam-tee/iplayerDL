{
  description = "iplayerDL - yt-dlp/ffmpeg wrapper for BBC iPlayer";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      uv2nix,
      pyproject-nix,
      pyproject-build-systems,
      ...
    }:
    let
      inherit (nixpkgs) lib;
      forEachSystem = lib.genAttrs [
        "x86_64-linux"
        "aarch64-linux"
      ];

      workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };

      packageFor =
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};

          pythonSet =
            (pkgs.callPackage pyproject-nix.build.packages {
              python = pkgs.python313;
            }).overrideScope
              (
                lib.composeManyExtensions [
                  pyproject-build-systems.overlays.wheel
                  (workspace.mkPyprojectOverlay { sourcePreference = "wheel"; })
                  # pycryptodomex ships C-extension wheels that don't work under Nix;
                  # substitute the nixpkgs build via the pyproject.nix interop hack.
                  (final: prev:
                    let
                      hacks = final.callPackage pyproject-nix.build.hacks { };
                    in {
                      pycryptodomex = hacks.nixpkgsPrebuilt {
                        from = pkgs.python313Packages.pycryptodomex;
                        prev = prev.pycryptodomex;
                      };
                    })
                ]
              );

          virtualenv = pythonSet.mkVirtualEnv "iplayerdl-venv" workspace.deps.all;
        in
        pkgs.runCommand "iplayerdl-0.1.0"
          {
            nativeBuildInputs = [ pkgs.makeWrapper ];
            meta.mainProgram = "iplayerdl";
            passthru = {
              pythonEnv = virtualenv;
            };
          }
          ''
            mkdir -p $out/bin
            for exe in ${virtualenv}/bin/*; do
              ln -s "$exe" "$out/bin/$(basename "$exe")"
            done
            wrapProgram $out/bin/iplayerdl \
              --prefix PATH : ${
                lib.makeBinPath [
                  pkgs.ffmpeg
                  pkgs.yt-dlp
                ]
              }
          '';
    in
    {
      packages = forEachSystem (
        system:
        { }
        // {
          default = packageFor system;
          iplayerdl = packageFor system;
        }
      );

      apps = forEachSystem (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/iplayerdl";
        };
      });

      checks = forEachSystem (system: {
        iplayerdl = self.packages.${system}.default;
      });

      devShells = forEachSystem (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          default = pkgs.mkShell {
            packages = with pkgs; [
              uv
              ruff
              ffmpeg
            ];
            env = {
              UV_PYTHON_DOWNLOADS = "never";
              UV_PYTHON = pkgs.python313.interpreter;
            };
            shellHook = ''
              unset PYTHONPATH
            '';
          };
        }
      );
    };
}
