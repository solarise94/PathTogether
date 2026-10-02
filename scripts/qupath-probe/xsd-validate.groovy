// args: [0]=xsd, [1..]=xml files. JDK javax.xml.validation against the OME 2016-06 schema.
import javax.xml.validation.SchemaFactory
import javax.xml.transform.stream.StreamSource
import javax.xml.XMLConstants
def schema = SchemaFactory.newInstance(XMLConstants.W3C_XML_SCHEMA_NS_URI).newSchema(new File(args[0]))
for (int i = 1; i < args.length; i++) {
    try { schema.newValidator().validate(new StreamSource(new File(args[i]))); println("XSD_VALID ${args[i]}") }
    catch (Throwable e) { println("XSD_INVALID ${args[i]} ${e}") }
}
// Bio-Formats' own validator as a second opinion
for (int i = 1; i < args.length; i++) {
    println("BF_XMLTools_valid ${args[i]} " + loci.common.xml.XMLTools.validateXML(new File(args[i]).text))
}
