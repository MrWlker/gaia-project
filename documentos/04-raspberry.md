# 4. Raspberry Pi: imagem do SD, rede e ligações

Alvo: **Raspberry Pi 3 B/B+ (1 GB)** com a **Intel RealSense D435** no USB e um
**DHT11** no GPIO.

## Por que Raspberry Pi OS Lite 64-bit *bullseye*

O `pyrealsense2` só publica wheel aarch64 para Python 3.8 e 3.9. O bullseye tem 3.9;
o bookworm tem 3.11 e obrigaria a compilar a librealsense, o que leva horas num A53.
O último Pi OS bullseye é o de 2025-05-06, e o script baixa e confere o SHA-256.

O bullseye saiu de suporte em 2026-08, e o repositório de segurança dele já anuncia
pacotes que não existem mais (404). Por isso a **instalação é 100% offline**: não usa
`apt`, `venv` nem `pip` no Pi.

## Gerar a imagem (no PC, sem root)

```bash
cd estacao/imagem
./montar_offline.sh      # uma vez, no PC com internet: wheels aarch64/cp39 + libusb
GAIA_ETH_IP=192.168.123.144/24 GAIA_ETH_GW=192.168.123.99 \
GAIA_SSID=MinhaRede GAIA_SENHA='...' ./gerar_imagem.sh      # -> gaia-pi.img
```

- `montar_offline.sh` baixa numpy 1.26 e Pillow 10.4 (as últimas com cp39),
  pyrealsense2, requests e a pilha do DHT como wheels aarch64. Baixa também a
  `libusb-1.0` do bullseye arm64, de que o pyrealsense2 precisa.
- `gerar_imagem.sh` só mexe na partição de boot (FAT, via mtools). A senha do Wi-Fi
  entra por variável de ambiente e não fica no git.

Gravar (confira o dispositivo: `dd` no disco errado apaga o disco):

```bash
lsblk -o NAME,SIZE,MODEL,TRAN
sudo dd if=gaia-pi.img of=/dev/sdX bs=4M conv=fsync status=progress
```

O `dd` parece travar no fim: é o `fsync` descarregando no cartão. Espere o prompt
voltar. **Não use o Raspberry Pi Imager com personalização**, porque ela sobrescreve o
`firstrun.sh`.

## O que a imagem faz sozinha

1. 1º boot: expande a partição e reinicia.
2. `firstrun.sh`: hostname `gaia-pi`, usuário `pi` / senha `gaia`, SSH, Wi-Fi,
   copia `/boot/gaia` para `/home/pi/gaia` e reinicia.
3. `gaia-instalar.service` roda `instalar_pi.sh`:
   - aplica o IP fixo da `eth0` (lido de `/boot/gaia-rede.conf`);
   - extrai os wheels em `lib/`;
   - regra udev da RealSense;
   - serviço `gaia-estacao`.

Tudo sem rede. Depois: `http://192.168.123.144:8080` (ou `http://gaia-pi.local:8080`).

Para trocar o IP sem regravar o SD, edite o arquivo `gaia-rede.conf` da partição
`boot` no PC:

```
ETH_IP=192.168.123.144/24
ETH_GW=192.168.123.99
ETH_DNS=192.168.123.99
```

O DNS padrão é o próprio gateway. Redes de laboratório costumam bloquear 1.1.1.1 e
8.8.8.8.

## Reinstalar num Pi que já está na rede

```bash
cd estacao
./implantar.sh pi@192.168.123.144      # copia código + wheels por ssh e instala
./implantar.sh vm                      # a VM do QEMU
```

Preserva `estacao.json`, a fila e o histórico do lado de lá.

## Rede

**Cabo direto no PC** (o jeito mais simples): o PC vira `192.168.123.99` e compartilha
a internet do Wi-Fi dele. Com o NetworkManager:

```bash
GAIA_WIFI_REDE=<conexão Wi-Fi do PC> servidor/cabo.sh ligar
servidor/cabo.sh status
```

**Hotspot do PC**: `servidor/hotspot.sh ligar`. É 2,4 GHz, porque o Pi 3 B não enxerga
5 GHz. Muita placa de notebook não faz AP e cliente ao mesmo tempo: ligar o hotspot
derruba o Wi-Fi do PC.

## Sem tela nem teclado: como saber se funcionou

- `servidor/achar_pi.sh -f` varre a rede do hotspot e diz a etapa: fora da rede → só
  SSH (instalando) → interface no ar. Ignora celulares pelo MAC.
- `ssh pi@<ip>` (senha `gaia`), e dentro: `journalctl -fu gaia-instalar`,
  `systemctl status gaia-estacao`.
- **Sem acesso nenhum**: tire o SD e leia no PC `gaia-firstrun.log`,
  `gaia-instalacao.log` e `gaia-rede.log`, na partição `boot`. O `rootfs/var/log/syslog`
  é legível por quem está no grupo `adm`.
- LED verde piscando irregular é boot; quase parado é sistema ocioso. LED vermelho
  piscando é fonte fraca.

## Ligação do DHT11 (Pi 3 B/B+)

```
             3V3  (1) (2)  5V
           GPIO2  (3) (4)  5V
           GPIO3  (5) (6)  GND   ◄── DHT11 GND
DHT11 DATA►GPIO4  (7) (8)  GPIO14
```

| DHT11 | Pi |
|---|---|
| VCC / + | pino 1, 3,3 V |
| DATA | pino 7, GPIO4 |
| GND / − | pino 6, GND |

- **Alimente com 3,3 V, não 5 V**: o pull-up levaria o DATA a 5 V, e o GPIO do Pi só
  aguenta 3,3 V.
- O módulo de 3 pinos numa plaquinha já tem pull-up. O DHT11 "pelado", de 4 pinos,
  precisa de 10 kΩ entre DATA e VCC.
- Sem `libgpiod`, a leitura cai para bit-bang pelo RPi.GPIO, que já vem no Pi OS.
  É menos preciso, mas o DHT tolera e repete.

## Cuidados com a D435 no Pi 3

- Só USB 2.0: 640×480 a 15 fps com cor + profundidade.
- A câmera puxa até ~700 mA. Use fonte de 2,5 A e, de preferência, um hub USB
  alimentado. Câmera reconectando sozinha é sintoma de energia.

## A VM "Pi 3" no QEMU

```bash
sudo apt install qemu-system-arm qemu-efi-aarch64 qemu-utils genisoimage
estacao/qemu/preparar_vm.sh     # Debian 11 arm64 + cloud-init (pi/gaia)
estacao/qemu/iniciar_vm.sh      # Cortex-A53 x4, 1 GB, D435 passada no xHCI; --fundo
estacao/implantar.sh vm
```

A VM usa a máquina `virt`, não a `raspi3b`: o USB emulado da raspi3b não passa a D435.
O que importa reproduzir do Pi 3 é o orçamento (A53, 4 núcleos, 1 GB, arm64).

Para testar só a instalação offline, sem VM: um container
`arm64v8/python:3.9-slim-bullseye` com `--network none` e o `qemu-user-static`
registrado. Foi assim que se descobriu que o pyrealsense2 precisa da libusb.
