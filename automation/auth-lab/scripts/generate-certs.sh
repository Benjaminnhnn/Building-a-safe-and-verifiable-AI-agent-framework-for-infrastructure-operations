#!/bin/sh
set -eu
openssl req -x509 -newkey rsa:3072 -nodes \
    -keyout /certs/ldap-ca.key -out /certs/ca.crt -days 3650 \
    -subj '/CN=Moodle Auth Lab Local CA' \
    -addext 'basicConstraints=critical,CA:TRUE' \
    -addext 'keyUsage=critical,keyCertSign,cRLSign'
openssl req -newkey rsa:2048 -nodes \
    -keyout /certs/ldap.key -out /tmp/ldap-server.csr \
    -subj '/CN=openldap'
printf 'subjectAltName=DNS:openldap\nextendedKeyUsage=serverAuth\nbasicConstraints=CA:FALSE\n' > /tmp/ldap-server.ext
openssl x509 -req -in /tmp/ldap-server.csr -CA /certs/ca.crt \
    -CAkey /certs/ldap-ca.key -CAcreateserial \
    -out /certs/ldap.crt -days 825 -extfile /tmp/ldap-server.ext
openssl dhparam -out /certs/dhparam.pem 2048
chmod 0600 /certs/ldap-ca.key /certs/ldap.key
