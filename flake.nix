{
  description = "Python development environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      # Define the architectures we want to support
      supportedSystems = [ "x86_64-linux" "aarch64-linux" ];
      
      # A helper function to generate outputs for each supported system
      forEachSupportedSystem = f: nixpkgs.lib.genAttrs supportedSystems (system: f {
        pkgs = import nixpkgs { inherit system; };
      });
    in
    {
      devShells = forEachSupportedSystem ({ pkgs }: {
        default = pkgs.mkShell {
          # 'packages' is preferred over 'buildInputs' for development tools in mkShell
          packages = with pkgs; [
            python314
            uv
            zlib
            glib
          ];

          # Endpoint/credential env vars are NOT declared here — they live in a
          # local, gitignored .env file (see .env.example). .envrc loads it via
          # `dotenv_if_exists .env`, so different operators can point at their
          # own infra without editing this flake.

          # We construct the LD_LIBRARY_PATH dynamically exactly as before
          shellHook = ''
            export LD_LIBRARY_PATH=${pkgs.lib.makeLibraryPath (with pkgs; [
              stdenv.cc.cc.lib
              zlib
              glib
            ])}:$LD_LIBRARY_PATH

            if [ ! -d ".venv" ]; then
              uv venv
            fi
            source .venv/bin/activate
            uv pip install -e .
          '';
        };
      });
    };
}
