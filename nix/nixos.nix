# NixOS module: runs the iplayerDL web interface as a systemd service.
#
# Example:
#   {
#     services.iplayerdl = {
#       enable = true;
#       port = 8080;
#       settings = {
#         urls = [ "https://www.bbc.co.uk/iplayer/episode/..." ];
#         folders.media_dir = "/mnt/data/media";
#         pipeline.allow_speculative_adds = true;
#       };
#       # Secrets stay out of the store: KEY=VALUE lines read by systemd.
#       environmentFile = "/run/secrets/iplayerdl.env";
#     };
#   }
#
# Note: keys set under settings.environment override the same keys from
# environmentFile (the app exports the config section into the process
# environment), so keep secrets in one place: environmentFile.
{ self, ... }:
{
  flake.nixosModules.default =
    {
      config,
      lib,
      pkgs,
      ...
    }:
    let
      cfg = config.services.iplayerdl;
      toml = pkgs.formats.toml { };
      generatedConfig = toml.generate "iplayerDL-config.toml" cfg.settings;
      configFile = if cfg.configFile != null then cfg.configFile else generatedConfig;
    in
    {
      options.services.iplayerdl = {
        enable = lib.mkEnableOption "iplayerDL web interface";

        package = lib.mkOption {
          type = lib.types.package;
          default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
          defaultText = lib.literalExpression "self.packages.\${pkgs.stdenv.hostPlatform.system}.default";
          description = "iplayerDL package to run.";
        };

        host = lib.mkOption {
          type = lib.types.str;
          default = "127.0.0.1";
          description = "Bind address for the web interface.";
        };

        port = lib.mkOption {
          type = lib.types.port;
          default = 8080;
          description = "Port for the web interface.";
        };

        user = lib.mkOption {
          type = lib.types.nullOr lib.types.str;
          default = null;
          description = ''
            User to run the service as. When null, a DynamicUser is used
            with state in /var/lib/iplayerdl.
          '';
        };

        group = lib.mkOption {
          type = lib.types.nullOr lib.types.str;
          default = null;
          description = "Group to run the service as (only used with user).";
        };

        settings = lib.mkOption {
          type = toml.type;
          default = { };
          description = ''
            iplayerDL configuration, rendered to TOML and passed as
            IPLAYERDL_CONFIG. Everything is optional (built-in defaults
            apply); keep secrets in environmentFile instead.
          '';
          example = {
            urls = [ "https://www.bbc.co.uk/iplayer/episode/..." ];
            folders.media_dir = "/mnt/data/media";
          };
        };

        configFile = lib.mkOption {
          type = lib.types.nullOr lib.types.path;
          default = null;
          description = ''
            Full config.toml to use instead of generating one from
            settings. Mutually exclusive with settings (this wins).
          '';
        };

        environmentFile = lib.mkOption {
          type = lib.types.nullOr lib.types.path;
          default = null;
          description = ''
            File with KEY=VALUE lines (e.g. an agenix/sops-nix secret)
            loaded into the service environment for API keys and
            passwords. Not stored in the Nix store.
          '';
          example = "/run/secrets/iplayerdl.env";
        };

        openFirewall = lib.mkOption {
          type = lib.types.bool;
          default = false;
          description = "Open the firewall for port.";
        };

        extraArgs = lib.mkOption {
          type = lib.types.listOf lib.types.str;
          default = [ ];
          description = "Extra arguments appended to the web command.";
          example = [ "--log-level" "DEBUG" ];
        };
      };

      config = lib.mkIf cfg.enable {
        assertions = [
          {
            assertion = cfg.configFile == null || cfg.settings == { };
            message = "services.iplayerdl: set either configFile or settings, not both.";
          }
        ];

        networking.firewall = lib.mkIf cfg.openFirewall {
          allowedTCPPorts = [ cfg.port ];
        };

        systemd.services.iplayerdl = {
          description = "iplayerDL web interface";
          after = [ "network-online.target" ];
          wants = [ "network-online.target" ];
          wantedBy = [ "multi-user.target" ];
          environment.IPLAYERDL_CONFIG = configFile;
          serviceConfig = {
            ExecStart = lib.concatStringsSep " " (
              [
                "${cfg.package}/bin/iplayerdl"
                "web"
                "--host"
                cfg.host
                "--port"
                (toString cfg.port)
              ]
              ++ cfg.extraArgs
            );
            WorkingDirectory = "/var/lib/iplayerdl";
            StateDirectory = "iplayerdl";
            DynamicUser = cfg.user == null;
            User = lib.mkIf (cfg.user != null) cfg.user;
            Group = lib.mkIf (cfg.group != null) cfg.group;
            Restart = "on-failure";
            RestartSec = "10s";
            # Light sandboxing only: media/download folders are user
            # configured and may live anywhere, so avoid ProtectSystem /
            # ProtectHome here (override via systemd.services.iplayerdl
            # if your paths allow it).
            PrivateTmp = true;
            NoNewPrivileges = true;
          }
          // lib.optionalAttrs (cfg.environmentFile != null) {
            EnvironmentFile = cfg.environmentFile;
          };
        };
      };
    };
}
