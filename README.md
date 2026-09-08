docker-compose.yml
services:
  db:
    image: postgres:15-alpine
    environment:
      POSTGRES_DB: postgres
      POSTGRES_USER: odoo
      POSTGRES_PASSWORD: 978@308.com
    volumes:
      - odoo-db-data-new:/var/lib/postgresql/data
    restart: unless-stopped

  odoo:
    image: odoo:17
    stop_grace_period: 1m
    depends_on:
      - db
    ports:
      - "8069:8069"
    environment:
      HOST: db
      USER: odoo
      PASSWORD: 978@308.com
    volumes:
      - ./addons:/mnt/extra-addons
      - ./odoo.conf:/etc/odoo/odoo.conf
      - odoo-web-data-new:/var/lib/odoo
    command: --config /etc/odoo/odoo.conf
    restart: unless-stopped

volumes:
  odoo-db-data-new:
  odoo-web-data-new:

odoo.conf
[options]
addons_path = /usr/lib/python3/dist-packages/odoo/addons,/mnt/extra-addons
admin_passwd = 978@308.com
proxy_mode = True
