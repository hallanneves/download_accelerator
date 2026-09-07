# Download Accelerator

Acelerador de download para arquivos grandes: abre várias conexões HTTP ao mesmo tempo (Range requests), cada uma baixa um pedaço e grava direto na posição certa do arquivo final. Só biblioteca padrão do Python, sem dependências.

*Multi-connection download accelerator with resume support and a native-looking macOS interface. Standard library only.*

## O que ele faz

- **Várias conexões em paralelo.** O arquivo é dividido em pedaços de 16 MiB e cada conexão baixa um pedaço por vez.
- **Conexões ajustáveis durante o download.** Aumentou, abre mais conexões na hora. Diminuiu, as que sobram largam o pedaço atual, que volta para a fila.
- **Retomada.** O progresso de cada pedaço fica em `<arquivo>.part.json` ao lado do `<arquivo>.part`. Se o download parar (cancelamento, app fechado, queda de energia), rodar de novo com o mesmo destino continua de onde parou. Antes de retomar ele confere tamanho, ETag e Last-Modified para garantir que o arquivo no servidor ainda é o mesmo.
- **Verificação SHA-256** opcional no fim.
- **Fallback** para uma conexão só quando o servidor não aceita Range.

## Interface gráfica

Feita em Tkinter seguindo as Human Interface Guidelines do macOS: fonte e cor de destaque do sistema, modo claro e escuro, tamanhos em base 10 como o Finder.

A lista **Downloads** é a memória do app: mostra o que foi concluído e o que ficou pela metade, com botão **Retomar**. Fica em `~/Library/Application Support/Download Accelerator/history.json`.

```
python3 download_accelerator_gui.py
```

No macOS com Python do Homebrew, o Tkinter vem separado:

```
brew install python-tk@3.14
```

Ou use o Python da Apple, que já tem Tkinter: `/usr/bin/python3 download_accelerator_gui.py`.

### App para o macOS

`build_app.sh` empacota a interface com o PyInstaller e instala em `/Applications/Download Accelerator.app`, com ícone próprio e sem depender do Python instalado.

```
./build_app.sh
```

## Linha de comando

```
python3 download_accelerator.py URL [-o SAIDA] [-n CONEXOES] [--chunk MiB] [--sha256 HASH] [--no-resume]
```

Exemplo com a ISO do Ubuntu 26.04.1 (6,5 GB):

```
python3 download_accelerator.py \
    https://releases.ubuntu.com/26.04.1/ubuntu-26.04.1-desktop-amd64.iso \
    -n 8 --sha256 601e30fbf5d97759367c632e2c33630665039b7e2158fd068403da3ccf1bda1f
```

`Ctrl+C` salva o progresso. Rodar o mesmo comando de novo retoma.

## Como funciona

1. Uma requisição com `Range: bytes=0-0` descobre o tamanho, se o servidor aceita Range, a URL final e o ETag.
2. O arquivo `.part` é pré-alocado com o tamanho total.
3. Um pool dinâmico de threads consome uma fila de pedaços. Cada thread pede `Range: bytes=inicio-fim` e grava com `os.pwrite` na posição certa.
4. A cada segundo o estado dos pedaços é salvo no `.part.json`.
5. No fim, o `.part` é renomeado para o nome final e o estado é apagado.

## Arquivos

| Arquivo | O que é |
| --- | --- |
| `download_accelerator.py` | Motor e linha de comando. Classe `Download` com `run()`, `set_connections(n)` e `cancel`. |
| `download_accelerator_gui.py` | Interface Tkinter. Importa o motor. |
| `build_app.sh` | Gera e instala o app do macOS. |
| `Abrir interface.command` | Duplo clique abre a interface direto dos `.py`. |
| `assets/icon.svg`, `assets/icon.icns` | Ícone do app. |

Testado com Python 3.14 (Homebrew, Tk 9.0) e Python 3.9 (Apple, Tk 8.5) no macOS 26.

## Licença

[PolyForm Noncommercial 1.0.0](LICENSE.md). Pode usar, copiar, modificar e redistribuir para qualquer fim não comercial: uso pessoal, pesquisa, ensino, organizações sem fins lucrativos e órgãos públicos. Uso comercial não é permitido sem autorização do autor.

Required Notice: Copyright 2026 Hallan Neves (https://github.com/hallanneves/download_accelerator)
