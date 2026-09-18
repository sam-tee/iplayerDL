# `nix run` entry point.
{
  perSystem = { config, ... }: {
    apps.default = {
      type = "app";
      program = "${config.packages.default}/bin/iplayerdl";
    };
  };
}
