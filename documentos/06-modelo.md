# 6. O modelo

O modelo pré-treinado **não está neste repositório**: ele é publicado no Hugging Face
e baixado para `modelo/`:

```bash
python3 servidor/baixar_modelo.py --repo <usuario>/<repositorio-do-modelo>
```

O código de treino e os dados acompanham o artigo.

## Entradas e saídas

- **Imagem**: RGB, redimensionada para 1,14× o tamanho de entrada e recortada no
  centro (384 px no ConvNeXt publicado). A borda da imagem não é vista pela rede.
- **Sensores** (opcionais): temperatura (°C), umidade do ar (%) e umidade do solo (%).
  Viram um vetor de 12 posições: valor normalizado, flag de presença, desvio da faixa
  ideal da espécie e flag do desvio. Sensor ausente tem flag 0, e não valor 0.
- **Saídas**, uma cabeça por eixo, cada uma com softmax próprio:

| eixo | classes |
|---|---|
| `species` | 1136 espécies |
| `condition` | `diseased`, `healthy` |
| `agent` | `none`, `fungus`, `oomycete`, `bacteria`, `virus`, `pest`, `abiotic` |
| `organ` | `leaf`, `flower`, `fruit`, `bark`, `habit`, `branch` |
| `hydration` | `hydrated`, `water_stress`, `waterlogged` |

O checkpoint lista em `trained_axes` quais cabeças tiveram rótulo no treino. As que
não estão lá (hoje, `hydration`) existem, mas a saída delas é ruído; o preditor marca
`"trained": false`.

## Arquivos publicados

| arquivo | conteúdo |
|---|---|
| `gaia.pt` | pesos + arquitetura + tamanho de entrada + classes + configuração dos sensores |
| `classes.json` | as classes, legíveis sem torch |
| `species_env.csv` | faixas ambientais por espécie, usadas no desvio de sensor (opcional) |
| `README.md` | cartão do modelo |

Sem o `species_env.csv`, a inferência funciona; só o "desvio da faixa ideal" entra
como ausente.

## Publicar (quem tem o checkpoint de treino)

```bash
python3 ferramentas/exportar_modelo.py caminho/best.pt publicar/ caminho/species_env.csv
# revise publicar/README.md (licença, dados, métricas, artigo)
huggingface-cli login
huggingface-cli upload <usuario>/<repositorio> publicar/ . --repo-type model
```

O exportador mantém só o que a inferência usa e descarta época, passo global e
métricas de validação.

O `.pt` é um pickle do PyTorch, que o preditor carrega com `weights_only=False`
porque o arquivo leva o mapa de classes junto. Carregue só checkpoints de fonte
confiável. Se preferir publicar em `safetensors`, separe os pesos da configuração.

## Limites que a interface e as respostas não escondem

- **Espécie**: a confiança é baixa fora das fotos de laboratório (folha isolada, fundo
  limpo). No canteiro, trate a espécie como sugestão; a identidade da planta vem da
  posição, não da espécie.
- **Caixas**: saem da máscara de vegetação, não de um detector treinado.
- **Vermelho do Grad-CAM**: mostra onde a rede se apoiou para "doente", não mede lesão.
- **Ramo de sensores**: nenhuma imagem de treino tinha leitura de sensor. O ramo foi
  construído para não atrapalhar (projeção final zerada e porta pela flag de
  presença) e aprende quando houver série da horta. Até lá, o efeito das leituras
  sobre a predição é pequeno.
