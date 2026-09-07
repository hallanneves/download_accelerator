# Changelog

## 0.1.0 (2026-09-07)

### O que tem

- **Várias conexões ao mesmo tempo.** O arquivo é dividido em pedaços de 16 MiB e cada conexão baixa um pedaço, gravando direto na posição certa do arquivo final.
- **Conexões ajustáveis durante o download.** Aumentou, abre mais na hora. Diminuiu, as que sobram largam o pedaço atual e ele volta para a fila.
- **Retomada.** O progresso fica salvo a cada segundo em `<arquivo>.part.json`. Cancelar, fechar o app ou faltar luz não perde o que já foi baixado. Antes de retomar ele confere tamanho, ETag e Last-Modified para garantir que o arquivo no servidor é o mesmo.
- **Lista de downloads** com botão Retomar para o que ficou pela metade.
- **Verificação SHA-256** opcional.
- **Linha de comando** com as mesmas funções, sem dependências além do Python.

### Instalar no macOS

1. Baixe o `Download-Accelerator-0.1.0-macos-arm64.zip` abaixo e descompacte. Serve para Mac com chip Apple.
2. Arraste o `Download Accelerator.app` para a pasta Aplicativos.
3. Na primeira abertura o macOS vai avisar que não conseguiu verificar o app, porque ele não é assinado com certificado da Apple. Vá em Ajustes do Sistema, Privacidade e Segurança, e clique em "Abrir Mesmo Assim". Só precisa fazer isso uma vez.

Ou, se preferir rodar direto do código:

```
brew install python-tk@3.14
python3 download_accelerator_gui.py
```

### Licença

PolyForm Noncommercial 1.0.0: livre para uso não comercial.
