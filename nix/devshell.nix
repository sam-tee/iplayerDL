# `nix develop` shell: build tools and media CLIs; Python packages
# stay managed by uv (see pyproject.toml).
{
  perSystem = { pkgs, ... }: {
    devShells.default = pkgs.mkShell {
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
  };
}
