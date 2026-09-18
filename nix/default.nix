# Entry point for all Nix configuration.
#
# flake.nix imports this directory; each file below contributes one
# slice of the flake outputs as a flake-parts module.
{
  imports = [
    ./package.nix
    ./apps.nix
    ./checks.nix
    ./devshell.nix
  ];
}
