# Vivaldi CLI

Consulta local e somente leitura ao Vivaldi no macOS, sem Raycast, extensão ou serviço de rede. Usa apenas a biblioteca padrão do Python 3.10+.

Projeto independente, sem afiliação com a Vivaldi Technologies.

Licença: [MIT](LICENSE).

Antes de haver um pacote publicado, execute diretamente com `python3 vivaldi.py <comando>` a partir deste diretório. Para disponibilizar o comando `vivaldi` no PATH, crie um link para `vivaldi.py` em um diretório do seu PATH, sem substituir um comando existente. Os exemplos abaixo usam esse nome de comando.

```sh
vivaldi profiles
vivaldi history "termo" --profile Default --since 2026-09-01 --domain example.com
vivaldi bookmarks "termo" --all-profiles
vivaldi downloads "pdf" --since 2026-09-01
vivaldi tabs "termo"
vivaldi stats --all-profiles --top 20 --json
```

Cada comando aceita `--json`; as listagens aceitam `--limit` (padrão 50, `0` para todos). Histórico, downloads e estatísticas aceitam `--since` e `--until` (datas locais, inclusivas); histórico, downloads e estatísticas aceitam `--domain`. Sem `--all-profiles`, a CLI usa `Default` ou o `--profile` informado. Com `--all-profiles`, histórico e downloads são ordenados por data antes de aplicar `--limit`. `--data-dir` permite apontar para outra pasta local de perfis. Consulte `vivaldi <comando> --help`.

Histórico e downloads são lidos de uma cópia temporária do banco SQLite; o perfil original nunca é aberto para escrita. As abas são consultadas por Apple Events e exigem Vivaldi aberto e permissão de automação do macOS. A consulta de abas mostra as janelas da instância acessível ao AppleScript, sem atribuir um perfil a cada aba. Não há cache persistente, leitura de senhas/cookies ou comandos de edição. As estatísticas contam visitas registradas, **não** tempo de uso. Dados recentes podem não aparecer se o navegador ainda não os tiver gravado no banco; o período disponível depende da configuração de retenção e do Sync local.

Para testar sem acessar dados pessoais: `python3 -m unittest discover -s tests -v`.
