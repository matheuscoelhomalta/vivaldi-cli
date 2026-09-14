# Vivaldi CLI

[English](README.md)

Consulta local e somente leitura aos dados do Vivaldi no macOS, sem Raycast, extensão, serviço de rede ou pacote Python de terceiros. Projeto independente, sem afiliação com a Vivaldi Technologies. [Licença MIT](LICENSE).

## Instalação

Instale pelo [tap do Homebrew](https://github.com/matheuscoelhomalta/homebrew-vivaldi-cli):

```sh
brew install matheuscoelhomalta/vivaldi-cli/vivaldi-cli
vivaldi --version
```

O Homebrew instala o Python necessário. Como alternativa, com Python 3.10 ou superior, execute `python3 vivaldi.py <comando>` neste repositório. Os exemplos abaixo usam o comando `vivaldi` instalado pelo Homebrew.

Para atualizar ou remover a instalação pelo Homebrew:

```sh
brew update
brew upgrade matheuscoelhomalta/vivaldi-cli/vivaldi-cli
brew uninstall matheuscoelhomalta/vivaldi-cli/vivaldi-cli
```

Execute apenas o comando desejado. `brew uninstall` remove a CLI, não os perfis do Vivaldi.

## Comandos

```sh
vivaldi profiles
vivaldi history "termo" --profile Default --since 2026-01-01 --domain example.com
vivaldi bookmarks "termo" --all-profiles
vivaldi downloads "pdf" --since 2026-01-01
vivaldi tabs "termo"
vivaldi stats --all-profiles --top 20 --json
```

Cada comando aceita `--json`. As listagens aceitam `--limit` (padrão: 50; `0` significa todos). Histórico, downloads e estatísticas aceitam `--since` e `--until` (datas locais inclusivas), além do filtro de domínio exato `--domain`. Sem `--all-profiles`, a CLI usa `Default` ou o perfil informado em `--profile`. Com `--all-profiles`, histórico e downloads são ordenados por data antes de aplicar `--limit`. Use `--data-dir` para outra pasta local de dados do Vivaldi. Consulte `vivaldi <comando> --help` para ver todas as opções.

Histórico e downloads são lidos de uma cópia temporária do banco SQLite do Vivaldi. O perfil original nunca é aberto para escrita. As abas são consultadas por Apple Events: o Vivaldi precisa estar aberto e o macOS pode pedir permissão para o aplicativo que executa a CLI controlar o navegador. O resultado mostra janelas acessíveis ao AppleScript, sem identificar o perfil de cada aba.

A CLI não possui cache persistente, não lê senhas ou cookies e não edita dados do navegador. As estatísticas contam visitas registradas, não tempo de uso. Dados recentes podem não aparecer até o Vivaldi gravá-los em disco; o histórico disponível depende da retenção e da sincronização local.

## Solução de problemas

- Se o Homebrew indicar que as Command Line Tools da Apple são incompatíveis, atualize-as pela Atualização de Software do macOS ou pelo [Apple Developer Downloads](https://developer.apple.com/download/all/). O Homebrew não atualiza essas ferramentas.
- Se `vivaldi profiles` não encontrar dados, abra o Vivaldi uma vez e confira a pasta do perfil, ou use `--data-dir`.
- Se `vivaldi tabs` falhar, abra o Vivaldi e permita o acesso a Apple Events em Privacidade e Segurança → Automação no macOS para o aplicativo que executa o comando.

## Desenvolvimento

Execute os testes sintéticos sem acessar perfis pessoais:

```sh
python3 -m unittest discover -s tests -v
```

O CI executa esses testes no macOS com Python 3.10–3.14. Ele não acessa perfis reais do Vivaldi.
