var f=class{constructor(e,t=""){this.client=e,this.prefix=t}async listNamespaces(){return(await this.client.request({method:"GET",path:`${this.prefix}/namespaces`})).data}};
