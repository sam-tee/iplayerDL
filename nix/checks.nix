# `nix flake check`: the package must build.
{
  perSystem = { config, ... }: {
    checks.iplayerdl = config.packages.default;
  };
}
