# The iplayerdl package: uv2nix-managed Python venv wrapped with
# ffmpeg/yt-dlp on PATH.
{ inputs, ... }:
{
  perSystem =
    {
      config,
      pkgs,
      lib,
      ...
    }:
    let
      workspace = inputs.uv2nix.lib.workspace.loadWorkspace {
        workspaceRoot = ../.;
      };

      pythonSet =
        (pkgs.callPackage inputs.pyproject-nix.build.packages {
          python = pkgs.python313;
        }).overrideScope
          (
            lib.composeManyExtensions [
              inputs.pyproject-build-systems.overlays.wheel
              (workspace.mkPyprojectOverlay { sourcePreference = "wheel"; })
              # pycryptodomex ships C-extension wheels that don't work under Nix;
              # substitute the nixpkgs build via the pyproject.nix interop hack.
              (final: prev:
                let
                  hacks = final.callPackage inputs.pyproject-nix.build.hacks { };
                in
                {
                  pycryptodomex = hacks.nixpkgsPrebuilt {
                    from = pkgs.python313Packages.pycryptodomex;
                    prev = prev.pycryptodomex;
                  };
                })
            ]
          );

      virtualenv = pythonSet.mkVirtualEnv "iplayerdl-venv" workspace.deps.all;
    in
    {
      packages = {
        iplayerdl = pkgs.runCommand "iplayerdl-0.1.0"
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
        default = config.packages.iplayerdl;
      };
    };
}
