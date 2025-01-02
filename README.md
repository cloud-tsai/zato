<p align="center">
  <a href="https://zato.io"><img alt="" src="https://zato.io/static/img/intro/banner.webp" /></a>
</p>

# Zato /zɑːtəʊ/

ESB, SOA, API and Cloud Integrations in Python.

Zato is a Python-based, open-source platform that lets you automate, integrate and orchestrate business systems,
APIs, workflows as well as hardware assets in industries such as
[airports](https://zato.io/en/industry/airports/index.html),
[defense](https://zato.io/en/industry/defense/index.html),
[health care](https://zato.io/en/industry/healthcare/index.html),
[telecommunications](https://zato.io/en/industry/telecom/index.html),
financial services,
government
and more.

<p align="center">
  <a href="https://zato.io"><img alt="ESB, API Integrations and Automation in Python" src="https://upcdn.io/kW15bqq/raw/root/static/img/intro/bus.png" /></a>
</p>

## Sample Python API service

```python
# -*- coding: utf-8 -*-

# Zato
from zato.server.service import Service

class SampleServiceREST(Service):
    """ A sample service that invokes a REST API endpoint.
    """
    def handle(self):

        # Python dict representing the payload we want to send across
        payload = {'billing':'395.7', 'currency':'USD'}

        # Obtains a connection object
        conn = self.out.rest['Billing'].conn

        # Invoke the resource providing all the information on input
        response = conn.post(self.cid, payload)

        # The response is auto-deserialized for us to a Python dict
        json_dict = response.data

        # Assign the returned dict to our response - Zato will serialize it to JSON
        # and our caller will get a JSON message from us.
        self.response.payload = json_dict
```

## Learn more

Visit https://zato.io for details, including:

* [Downloads](https://zato.io/en/docs/3.2/admin/guide/install/index.html)
* [Screenshots](https://zato.io/en/docs/3.2/intro/screenshots.html)
* [Programming examples](https://zato.io/en/docs/3.2/dev/index.html)
